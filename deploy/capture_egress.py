import ipaddress
import os
import re
import select
import socket
import socketserver
import sys


SUFFIXES = (
    'xhslink.cn', 'xiaohongshu.com', 'xhscdn.com',
    'douyin.com', 'iesdouyin.com', 'douyinpic.com', 'douyinstatic.com',
    'douyinvod.com', 'douyincdn.com', 'byteimg.com', 'bytedstatic.com', 'ibytedtos.com',
    'fonts.bytedance.com',
    'lf3-cdn-tos.bdxiguastatic.com',
    'lf3-cdn-tos.bytescm.com',
    'lf-douyin-mobile.bytecdn.com',
    'lf-c-flwb.bytetos.com',
    'lf-cdn-tos.bytescm.com',
    'lf-headquarters-speed.yhgfb-cn-static.com',
    'lf-rc1.yhgfb-cn-static.com',
    'lf-rc2.yhgfb-cn-static.com',
    'lf-ucenter-web.yhgfb-cn-static.com',
)


def relay(a, b):
    while True:
        ready, _, _ = select.select([a, b], [], [], 24)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (b if source is a else a).sendall(data)


def open_public(host):
    for family, kind, proto, _, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_reserved or ip.is_multicast
                or (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
            continue
        candidate = socket.socket(family, kind, proto)
        candidate.settimeout(4)
        try:
            candidate.connect(address)
            candidate.settimeout(12)
            return candidate
        except OSError:
            candidate.close()
    raise OSError('No allowed address')


class Connect(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(12)
        try:
            line = self.rfile.readline(1025)
            parsed = re.fullmatch(br'CONNECT ([a-zA-Z0-9.-]{1,253}):443 HTTP/1\.[01]\r\n', line)
            host = parsed[1].decode('ascii').lower() if parsed else ''
            if not any(host == s or host.endswith('.' + s) for s in SUFFIXES):
                self.wfile.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
                return
            for _ in range(32):
                header = self.rfile.readline(2049)
                if header == b'\r\n':
                    break
                if not header or len(header) > 2048:
                    return
            else:
                return
            try:
                target = open_public(host)
            except OSError:
                self.wfile.write(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n')
                return
            with target:
                self.wfile.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                relay(self.connection, target)
        except OSError:
            return


class Bridge(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.socket(socket.AF_UNIX) as local:
                self.request.settimeout(12)
                local.settimeout(12)
                local.connect(SOCKET_PATH)
                relay(self.request, local)
        except OSError:
            return


class UnixProxy(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class LocalBridge(socketserver.ThreadingTCPServer):
    daemon_threads = True


if __name__ == '__main__':
    operation, SOCKET_PATH = sys.argv[1:]
    if operation == 'proxy':
        with UnixProxy(SOCKET_PATH, Connect) as server:
            os.chmod(SOCKET_PATH, 0o600)
            server.serve_forever()
    elif operation == 'bridge':
        with LocalBridge(('127.0.0.1', 18080), Bridge) as server:
            server.serve_forever()
