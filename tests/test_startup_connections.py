"""Owned loopback cold-start connection burst; no application data or assets."""
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler

import server


class StartupConnections(unittest.TestCase):
    def test_browser_asset_burst_queues_before_accept_loop(self):
        class StaticFixture(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'owned startup asset'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        httpd = server.ServiceHTTPServer(('127.0.0.1', 0), StaticFixture)
        sockets = []
        thread = None
        try:
            # Frontend scripts/styles may arrive while the cold accept loop is
            # being scheduled. A refused script leaves a permanently blank page.
            for index in range(16):
                try:
                    client = socket.create_connection(httpd.server_address, timeout=.5)
                except OSError as error:
                    self.fail('Cold asset connection %d was refused: %s' % (index, type(error).__name__))
                sockets.append(client)
                client.sendall(b'GET /owned-asset.js HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n')
            thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            for client in sockets:
                client.settimeout(3)
                received = b''
                while chunk := client.recv(4096):
                    received += chunk
                self.assertTrue(received.startswith(b'HTTP/1.0 200'))
                self.assertTrue(received.endswith(b'owned startup asset'))
        finally:
            for client in sockets:
                client.close()
            if thread is not None:
                httpd.shutdown()
                thread.join(3)
                self.assertFalse(thread.is_alive())
            httpd.server_close()


if __name__ == '__main__':
    unittest.main()
