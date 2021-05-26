# from prometheus_client/exposition.py

from __future__ import unicode_literals

import copy, threading

from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, quote_plus, urlparse
from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer

from prometheus_client.registry import REGISTRY
from prometheus_client.exposition import choose_encoder


class SilentException(Exception):
    """This is used to fail a request with status 5xx"""
    pass

def registry_view_factory(parent, path, params):
    if not path.startswith('/probe'):
        return parent
    class ViewRestrictedRegistry(object):
        def __init__(self, parent, path, params):
            self.parent, self.path, self.params = parent, path, params
        def collect(self):
            collectors = None
            ti = None
            with parent._lock:
                collectors = copy.copy(parent._collector_to_names)
                if parent._target_info:
                    ti = parent._target_info_metric()
            if ti:
                yield ti

            for collector in collectors:
                collect2_func = None
                try:
                    collect2_func = collector.collect2
                except AttributeError:
                    pass
                if collect2_func:
                    for metric in collector.collect2(path, params):
                        yield metric
                # the only zero-argument collect collectors we have here are the default ones
                # and they should not be emitted for /probe queries
                #else:
                #    for metric in collector.collect():
                #        yield metric
    return ViewRestrictedRegistry(parent, path, params)


def _bake_output(registry, accept_header, path, params, registry_view_factory):
    """Bake output for metrics output."""
    encoder, content_type = choose_encoder(accept_header)
    try:
        if 'name[]' in params:
            registry = registry.restricted_registry(params['name[]'])
        if registry_view_factory:
            registry = registry_view_factory(registry, path, params)
        output = encoder(registry)
        return str('200 OK'), (str('Content-Type'), content_type), output
    except SilentException:
        return str('503 Server Error'), (str('Content-Type'), content_type), b''


def make_wsgi_app(registry=REGISTRY, registry_view_factory=registry_view_factory):
    """Create a WSGI app which serves the metrics from a registry."""

    def prometheus_app(environ, start_response):
        # Prepare parameters
        accept_header = environ.get('HTTP_ACCEPT')
        path = environ['PATH_INFO']
        params = parse_qs(environ.get('QUERY_STRING', ''))
        if path == '/favicon.ico':
            # Serve empty response for browsers
            status = '200 OK'
            header = ('', '')
            output = b''
        elif path == '/':
            status = '200 OK'
            header = ('', '')
            output = b'''<html><head><title>Porter</title></head><body>
Someday this will be a form. Today is not that day.
</body></html>'''
        elif path == '/config':
            status = '200 OK'
            header = ('', '')
            output = b'''<html><head><title>Porter Config</title></head><body>
Someday this will show the server's config.
</body></html>'''
        else: # /metrics or /probe
            status, header, output = _bake_output(registry, accept_header, path, params, registry_view_factory)

        start_response(status, [header])
        return [output]

    return prometheus_app

class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Thread per request HTTP server."""
    # Make worker threads "fire and forget". Beginning with Python 3.7 this
    # prevents a memory leak because ``ThreadingMixIn`` starts to gather all
    # non-daemon threads in a list in order to join on them at server close.
    daemon_threads = True

class _SilentHandler(WSGIRequestHandler):
    """WSGI handler that does not log requests."""

    def log_message(self, format, *args):
        """Log nothing."""

def start_wsgi_server(port, addr='', registry=REGISTRY, registry_view_factory=registry_view_factory):
    """Starts a WSGI server for prometheus metrics as a daemon thread."""
    app = make_wsgi_app(registry, registry_view_factory)
    httpd = make_server(addr, port, app, ThreadingWSGIServer, handler_class=_SilentHandler)
    t = threading.Thread(target=httpd.serve_forever)
    t.daemon = True
    t.start()
