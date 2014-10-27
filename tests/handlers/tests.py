# -*- coding: utf-8 -*-
from __future__ import unicode_literals


from django.core.handlers.wsgi import WSGIHandler, WSGIRequest
from django.core.signals import request_started, request_finished
from django.db import close_old_connections, connection
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test import override_settings
from django.utils.encoding import force_str
from django.utils import six


class HandlerTests(TestCase):

    def setUp(self):
        request_started.disconnect(close_old_connections)

    def tearDown(self):
        request_started.connect(close_old_connections)

    # Mangle settings so the handler will fail
    @override_settings(MIDDLEWARE_CLASSES=42)
    def test_lock_safety(self):
        """
        Tests for bug #11193 (errors inside middleware shouldn't leave
        the initLock locked).
        """
        # Try running the handler, it will fail in load_middleware
        handler = WSGIHandler()
        self.assertEqual(handler.initLock.locked(), False)
        with self.assertRaises(Exception):
            handler(None, None)
        self.assertEqual(handler.initLock.locked(), False)

    def test_bad_path_info(self):
        """Tests for bug #15672 ('request' referenced before assignment)"""
        environ = RequestFactory().get('/').environ
        environ['PATH_INFO'] = b'\xed' if six.PY2 else '\xed'
        handler = WSGIHandler()
        response = handler(environ, lambda *a, **k: None)
        self.assertEqual(response.status_code, 404)

    def test_non_ascii_query_string(self):
        """
        Test that non-ASCII query strings are properly decoded (#20530, #22996).
        """
        environ = RequestFactory().get('/').environ
        raw_query_strings = [
            b'want=caf%C3%A9',  # This is the proper way to encode 'café'
            b'want=caf\xc3\xa9',  # UA forgot to quote bytes
            b'want=caf%E9',  # UA quoted, but not in UTF-8
            b'want=caf\xe9',  # UA forgot to convert Latin-1 to UTF-8 and to quote (typical of MSIE)
        ]
        got = []
        for raw_query_string in raw_query_strings:
            if six.PY3:
                # Simulate http.server.BaseHTTPRequestHandler.parse_request handling of raw request
                environ['QUERY_STRING'] = str(raw_query_string, 'iso-8859-1')
            else:
                environ['QUERY_STRING'] = raw_query_string
            request = WSGIRequest(environ)
            got.append(request.GET['want'])
        if six.PY2:
            self.assertListEqual(got, ['café', 'café', 'café', 'café'])
        else:
            # On Python 3, %E9 is converted to the unicode replacement character by parse_qsl
            self.assertListEqual(got, ['café', 'café', 'caf\ufffd', 'café'])

    def test_non_ascii_cookie(self):
        """Test that non-ASCII cookies set in JavaScript are properly decoded (#20557)."""
        environ = RequestFactory().get('/').environ
        raw_cookie = 'want="café"'
        if six.PY3:
            raw_cookie = raw_cookie.encode('utf-8').decode('iso-8859-1')
        environ['HTTP_COOKIE'] = raw_cookie
        request = WSGIRequest(environ)
        # If would be nicer if request.COOKIES returned unicode values.
        # However the current cookie parser doesn't do this and fixing it is
        # much more work than fixing #20557. Feel free to remove force_str()!
        self.assertEqual(request.COOKIES['want'], force_str("café"))

    def test_invalid_unicode_cookie(self):
        """
        Invalid cookie content should result in an absent cookie, but not in a
        crash while trying to decode it (#23638).
        """
        environ = RequestFactory().get('/').environ
        environ['HTTP_COOKIE'] = 'x=W\x03c(h]\x8e'
        request = WSGIRequest(environ)
        # We don't test COOKIES content, as the result might differ between
        # Python version because parsing invalid content became stricter in
        # latest versions.
        self.assertIsInstance(request.COOKIES, dict)


class TransactionsPerRequestTests(TransactionTestCase):

    available_apps = []
    urls = 'handlers.urls'

    def test_no_transaction(self):
        response = self.client.get('/in_transaction/')
        self.assertContains(response, 'False')

    def test_auto_transaction(self):
        old_atomic_requests = connection.settings_dict['ATOMIC_REQUESTS']
        try:
            connection.settings_dict['ATOMIC_REQUESTS'] = True
            response = self.client.get('/in_transaction/')
        finally:
            connection.settings_dict['ATOMIC_REQUESTS'] = old_atomic_requests
        self.assertContains(response, 'True')

    def test_no_auto_transaction(self):
        old_atomic_requests = connection.settings_dict['ATOMIC_REQUESTS']
        try:
            connection.settings_dict['ATOMIC_REQUESTS'] = True
            response = self.client.get('/not_in_transaction/')
        finally:
            connection.settings_dict['ATOMIC_REQUESTS'] = old_atomic_requests
        self.assertContains(response, 'False')


class SignalsTests(TestCase):
    urls = 'handlers.urls'

    def setUp(self):
        self.signals = []
        request_started.connect(self.register_started)
        request_finished.connect(self.register_finished)

    def tearDown(self):
        request_started.disconnect(self.register_started)
        request_finished.disconnect(self.register_finished)

    def register_started(self, **kwargs):
        self.signals.append('started')

    def register_finished(self, **kwargs):
        self.signals.append('finished')

    def test_request_signals(self):
        response = self.client.get('/regular/')
        self.assertEqual(self.signals, ['started', 'finished'])
        self.assertEqual(response.content, b"regular content")

    def test_request_signals_streaming_response(self):
        response = self.client.get('/streaming/')
        self.assertEqual(self.signals, ['started'])
        self.assertEqual(b''.join(response.streaming_content), b"streaming content")
        self.assertEqual(self.signals, ['started', 'finished'])


class HandlerSuspiciousOpsTest(TestCase):
    urls = 'handlers.urls'

    def test_suspiciousop_in_view_returns_400(self):
        response = self.client.get('/suspicious/')
        self.assertEqual(response.status_code, 400)


@override_settings(ROOT_URLCONF='handlers.urls')
class HandlerNotFoundTest(TestCase):

    def test_invalid_urls(self):
        response = self.client.get('~%A9helloworld')
        self.assertEqual(response.status_code, 404)

        response = self.client.get('d%aao%aaw%aan%aal%aao%aaa%aad%aa/')
        self.assertEqual(response.status_code, 404)

        response = self.client.get('/%E2%99%E2%99%A5/')
        self.assertEqual(response.status_code, 404)

    def test_get_path(self):
        """
        Check if get_path() is working fine.
        """
        from django.utils.six.moves.urllib.parse import unquote
        from django.core.handlers.wsgi import get_path
        if six.PY3:
            from urllib.parse import unquote_to_bytes as unquote

        self.assertEqual(get_path(unquote('~%A9helloworld'.encode('utf-8'))), '~%A9helloworld')
        self.assertEqual(get_path(unquote('d%aao%aaw%aan%aal%aao%aaa%aad%aa/'.encode('utf-8'))), 'd%AAo%AAw%AAn%AAl%AAo%AAa%AAd%AA/')
        self.assertEqual(get_path(unquote('/%E2%99%E2%99%A5/'.encode('utf-8'))), '/%E2%99\u2665/')
        self.assertEqual(get_path(unquote('/%E2%99%A5'.encode('utf-8'))), '/\u2665')
        self.assertEqual(get_path(unquote('/%E2%98%80%E2%99%A5/'.encode('utf-8'))), '/\u2600\u2665/')
        self.assertEqual(get_path(unquote('/%E2%98%8E%E2%A9%E2%99%A5/'.encode('utf-8'))), '/\u260e%E2%A9\u2665/')
        self.assertEqual(get_path(unquote('/%2F%25?q=%C3%B6&x=%3D%25#%25'.encode('utf-8'))), '//%?q=\xf6&x==%#%')
        self.assertEqual(get_path(unquote('/%E2%98%90%E2%98%9A%E2%98%A3'.encode('utf-8'))), '/\u2610\u261a\u2623')
        self.assertEqual(get_path(unquote('/%E2%99%BF%99☃%E2%99%A3%E2%98%BD%A9'.encode('utf-8'))), '/\u267f%99\u2603\u2663\u263d%A9')
        self.assertEqual(get_path(unquote('/%E2%98%90/fred?utf8=%E2%9C%93'.encode('utf-8'))), '/\u2610/fred?utf8=\u2713')
        self.assertEqual(get_path(unquote('/%A7%25%10%98%25'.encode('utf-8'))), '/%A7%\x10%98%')
        self.assertEqual(get_path(unquote('/\xe2\x98\x90/fred?utf8=\xe2\x9c\x93'.encode('utf-8'))), '/\xe2\x98\x90/fred?utf8=\xe2\x9c\x93')
        self.assertEqual(get_path(unquote('/üsername'.encode('utf-8'))), '/\xfcsername')
        self.assertEqual(get_path(unquote('/üser:pässword@☃'.encode('utf-8'))), '/\xfcser:p\xe4ssword@\u2603')
        self.assertEqual(get_path(unquote('/%3Fmeh?foo=%26%A9'.encode('utf-8'))), '/?meh?foo=&%A9')
        self.assertEqual(get_path(unquote('/%E2%A8%87%87%A5%E2%A8%A0'.encode('utf-8'))), '/\u2a07%87%A5\u2a20')
        self.assertEqual(get_path(unquote('/你好'.encode('utf-8'))), '/\u4f60\u597d')
