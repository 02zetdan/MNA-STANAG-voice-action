"""
NMEA producer: replays the captured PCAP back onto multicast for parser.py.

Reads `src/kraken_data/multicast_AdvNavData-*.pcap`, walks each Ethernet
→ IPv4 → UDP frame, extracts the payload, and re-emits it onto the same
multicast group/port as a fresh UDP packet. Loops at the original
inter-packet timing (or accelerated via `--rate`) until interrupted.

Stdlib only — manual PCAP parsing avoids pulling in `scapy` / `dpkt`.

Usage:
    python pcap_replay.py [--pcap PATH] [--group 239.192.43.79]
                          [--port 4379] [--rate 1.0] [--loop]

`--rate 1.0` = real-time (default). `--rate 10.0` = 10× speedup. If
`--loop` is set the file is replayed end-to-end indefinitely.
"""

from __future__ import annotations

import argparse
import logging
import socket
import struct
import sys
import time
from pathlib import Path

logger = logging.getLogger("pcap_replay")

# PCAP file format: 24-byte global header, then a sequence of
# (16-byte record header) + (captured-bytes payload).
PCAP_GLOBAL_HEADER_LEN = 24
PCAP_RECORD_HEADER_LEN = 16

# Ethernet (DIX): 14 bytes (6 dst + 6 src + 2 type)
ETH_TYPE_IPV4 = 0x0800


def _iter_udp_payloads(pcap_path: Path):
    """Yield (relative_ts_seconds, src_ip, src_port, dst_ip, dst_port, payload)
    for every UDP frame in the PCAP. `relative_ts_seconds` is offset from
    the first frame's timestamp.

    Skips non-IPv4 / non-UDP / truncated packets silently.
    """
    with pcap_path.open("rb") as f:
        global_hdr = f.read(PCAP_GLOBAL_HEADER_LEN)
        if len(global_hdr) < PCAP_GLOBAL_HEADER_LEN:
            return
        # Magic byte order; we only support little-endian (0xa1b2c3d4 swapped).
        magic = global_hdr[:4]
        if magic == b"\xd4\xc3\xb2\xa1":
            endian = "<"
        elif magic == b"\xa1\xb2\xc3\xd4":
            endian = ">"
        else:
            raise RuntimeError(f"unrecognised PCAP magic: {magic.hex()}")

        first_ts: float | None = None
        while True:
            rec_hdr = f.read(PCAP_RECORD_HEADER_LEN)
            if len(rec_hdr) < PCAP_RECORD_HEADER_LEN:
                return
            ts_s, ts_us, cap_len, _orig_len = struct.unpack(endian + "IIII", rec_hdr)
            pkt = f.read(cap_len)
            if len(pkt) < cap_len:
                return
            ts = ts_s + ts_us / 1_000_000.0
            if first_ts is None:
                first_ts = ts
            rel_ts = ts - first_ts

            # Ethernet
            if len(pkt) < 14:
                continue
            eth_type = struct.unpack("!H", pkt[12:14])[0]
            if eth_type != ETH_TYPE_IPV4:
                continue

            # IPv4
            if len(pkt) < 14 + 20:
                continue
            ip = pkt[14:]
            ihl = (ip[0] & 0x0F) * 4  # IP header length in bytes
            if ihl < 20 or len(ip) < ihl + 8:
                continue
            proto = ip[9]
            if proto != 17:  # UDP
                continue
            src_ip = ".".join(str(b) for b in ip[12:16])
            dst_ip = ".".join(str(b) for b in ip[16:20])

            # UDP
            udp = ip[ihl:]
            sport, dport, ulen, _csum = struct.unpack("!HHHH", udp[:8])
            payload = udp[8:ulen] if ulen >= 8 else udp[8:]
            yield rel_ts, src_ip, sport, dst_ip, dport, payload


def replay(pcap_path: Path, group: str, port: int, *,
           rate: float = 1.0, loop: bool = False, iface: str = "127.0.0.1") -> None:
    """Replay PCAP UDP payloads onto (group, port).

    `iface` pins the outbound interface for multicast via IP_MULTICAST_IF.
    Defaults to 127.0.0.1 so single-host demos work without an external
    network. Set to "0.0.0.0" to let the kernel route by destination.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                    socket.inet_aton(iface))
    # Make sure local sockets see our multicast (default 1, but assert it).
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)

    iteration = 0
    while True:
        iteration += 1
        sent = 0
        skipped = 0
        wall_start = time.monotonic()
        for rel_ts, _sip, _sp, _dip, _dp, payload in _iter_udp_payloads(pcap_path):
            if not payload:
                skipped += 1
                continue
            # Pace to the original timing (scaled by `rate`).
            target_wall = wall_start + (rel_ts / max(rate, 0.0001))
            now = time.monotonic()
            if target_wall > now:
                time.sleep(target_wall - now)
            try:
                sock.sendto(payload, (group, port))
                sent += 1
            except OSError as e:
                logger.warning("sendto failed: %s", e)
        logger.info("pass %d: sent=%d skipped=%d", iteration, sent, skipped)
        if not loop:
            break


def main() -> int:
    p = argparse.ArgumentParser(description="Replay a captured PCAP onto multicast.")
    default_pcap = Path(__file__).resolve().parent.parent / "src" / "kraken_data"
    matches = sorted(default_pcap.glob("multicast_AdvNavData-*.pcap")) if default_pcap.is_dir() else []
    default_pcap_path = str(matches[0]) if matches else ""

    p.add_argument("--pcap", default=default_pcap_path,
                   help="Path to the PCAP. Defaults to the first "
                        "multicast_AdvNavData-*.pcap under src/kraken_data/.")
    p.add_argument("--group", default="239.192.43.79")
    p.add_argument("--port", type=int, default=4379)
    p.add_argument("--rate", type=float, default=1.0,
                   help="Replay speed multiplier. 1.0 = real-time, 10.0 = 10× faster.")
    p.add_argument("--loop", action="store_true",
                   help="Loop forever — restart the file on EOF.")
    p.add_argument("--iface", default="127.0.0.1",
                   help="Outbound multicast interface (IP_MULTICAST_IF). "
                        "Default: 127.0.0.1 for single-host demos. Use "
                        "0.0.0.0 to let the kernel route by destination.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[pcap_replay] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.pcap:
        print("error: no --pcap path given and no default PCAP found", file=sys.stderr)
        return 1
    pcap_path = Path(args.pcap)
    if not pcap_path.is_file():
        print(f"error: PCAP not found: {pcap_path}", file=sys.stderr)
        return 1

    logger.info("replaying %s → %s:%d via %s (rate=%.1fx, loop=%s)",
                pcap_path.name, args.group, args.port, args.iface, args.rate, args.loop)
    replay(pcap_path, args.group, args.port,
           rate=args.rate, loop=args.loop, iface=args.iface)
    return 0


if __name__ == "__main__":
    sys.exit(main())
