/**
 * Claude Connect — WebSocket TCP tunnel for Cloudflare Workers.
 *
 * Acts as a general-purpose CONNECT proxy for geo-bypass.
 * Lineman sends: GET wss://claude-connect.bshevelev75.workers.dev?target=host:port
 * Worker opens TCP to target via cloudflare:sockets, relays bytes over WebSocket.
 *
 * Blocked: Cloudflare's own CDN IPs (anti-loop restriction prevents connecting
 * to sites hosted on CF CDN from within a Worker). Route those via iProyal instead.
 */

import { connect } from 'cloudflare:sockets';

// Block internal Cloudflare infrastructure to avoid routing loops.
// Regular sites ON Cloudflare CDN are NOT blocked — only CF internal hostnames.
const BLOCKED_HOSTS = [
  'workers.cloudflare.com',
  'cloudflare.com',
  'workers.dev',
];

function isBlocked(host) {
  return BLOCKED_HOSTS.some(h => host === h || host.endsWith('.' + h));
}

export default {
  async fetch(request) {
    const upgrade = request.headers.get('Upgrade');
    if (!upgrade || upgrade.toLowerCase() !== 'websocket') {
      return new Response(
        'Claude Connect — General TCP Tunnel\nUse WebSocket with ?target=host:port\n',
        { status: 200, headers: { 'Content-Type': 'text/plain' } }
      );
    }

    const url = new URL(request.url);
    const target = url.searchParams.get('target');
    if (!target) {
      return new Response('Missing ?target=host:port', { status: 400 });
    }

    const colonIdx = target.lastIndexOf(':');
    const host = colonIdx > 0 ? target.substring(0, colonIdx) : target;
    const port = colonIdx > 0 ? parseInt(target.substring(colonIdx + 1), 10) : 443;

    if (isBlocked(host)) {
      return new Response(`Blocked: ${host}`, { status: 403 });
    }

    const { 0: client, 1: server } = new WebSocketPair();
    server.accept();

    const handleTunnel = async () => {
      const socket = connect({ hostname: host, port });

      const { writable: qWritable, readable: qReadable } = new TransformStream();
      const qWriter = qWritable.getWriter();

      server.addEventListener('message', ({ data }) => {
        const bytes =
          data instanceof ArrayBuffer
            ? new Uint8Array(data)
            : typeof data === 'string'
            ? new TextEncoder().encode(data)
            : data;
        qWriter.write(bytes).catch(() => {});
      });

      server.addEventListener('close', () => qWriter.close().catch(() => {}));
      server.addEventListener('error', () => qWriter.close().catch(() => {}));

      const pipeWsToTcp = qReadable.pipeTo(socket.writable).catch(() => {});

      const pipeTcpToWs = (async () => {
        const reader = socket.readable.getReader();
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            // Send Uint8Array directly — value.buffer may span a larger backing buffer
            server.send(value);
          }
        } finally {
          reader.releaseLock();
          server.close(1000, 'upstream closed');
        }
      })();

      await Promise.allSettled([pipeWsToTcp, pipeTcpToWs]);
      try { socket.close(); } catch (_) {}
    };

    handleTunnel().catch(err => {
      try { server.close(1011, String(err)); } catch (_) {}
    });

    return new Response(null, { status: 101, webSocket: client });
  },
};
