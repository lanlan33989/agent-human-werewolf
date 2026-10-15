# -*- coding: utf-8 -*-
"""
用 HTTPS 启动 P0-talk。

为什么需要它：
  浏览器只在「安全上下文」下允许网页访问麦克风。安全上下文 =
  HTTPS，或者 localhost / 127.0.0.1。
  所以用局域网 IP（192.168.x.x）+ http 打开时，getUserMedia 直接不可用，
  换浏览器、换手机都一样 —— 这不是代码问题。

用法：
    ./.venv/Scripts/python.exe run_https.py
    ./.venv/Scripts/python.exe run_https.py --port 8443

首次运行会自动生成自签名证书（cert.pem / key.pem，SAN 含本机所有 IP）。
手机打开 https://<你的IP>:8443 会提示证书不受信任，选择「继续访问」即可。
"""
import argparse
import datetime
import ipaddress
import socket
import sys
from pathlib import Path

BASE = Path(__file__).parent
CERT = BASE / "cert.pem"
KEY = BASE / "key.pem"


def local_ips() -> list[str]:
    """尽量把本机所有 IPv4 找出来，塞进证书的 SAN，免得换个网段就失效。"""
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def make_cert(ips: list[str]) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "p0-talk")])

    sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    for ip in ips:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )

    KEY.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--force-cert", action="store_true", help="重新生成证书")
    args = ap.parse_args()

    ips = local_ips()
    if args.force_cert or not (CERT.exists() and KEY.exists()):
        try:
            make_cert(ips)
        except ImportError:
            print("缺少 cryptography，请先执行：")
            print("  ./.venv/Scripts/python.exe -m pip install cryptography")
            return 1
        print("已生成自签名证书 cert.pem / key.pem")
        print("证书覆盖的地址：" + "、".join(["localhost", "127.0.0.1"] + ips))

    print("\n手机浏览器打开（注意是 https，且要带上 https 的端口）：")
    for ip in ips or ["<你的局域网IP>"]:
        print(f"  https://{ip}:{args.port}")
    print("\n手机会提示证书不受信任 —— 选择「继续访问 / 高级 → 继续」即可。")
    print("这是自签名证书的正常提示，不是错误。\n")

    import uvicorn
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        ssl_certfile=str(CERT),
        ssl_keyfile=str(KEY),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
