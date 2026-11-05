"""密钥只通过 stdin / stdout 在内存和 macOS 钥匙串之间传递。"""
import hmac
import subprocess
import sys
from uuid import uuid4

from .parser import InputError


class Keychain:
    def __init__(self, service="file2bug.app-secret"):
        self.service = service

    def get(self, reference: str) -> str:
        if sys.platform != "darwin":
            raise InputError("密钥管理需要 macOS 钥匙串。")
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", reference, "-w"],
                capture_output=True, timeout=30,
            )
        except (subprocess.SubprocessError, OSError):
            raise InputError("无法读取飞书密钥，请在本机解锁 macOS 钥匙串后重试。") from None
        if result.returncode:
            raise InputError("无法读取飞书密钥，请在本机解锁 macOS 钥匙串后重试。")
        return result.stdout.decode().rstrip("\n")

    def put(self, secret: str) -> str:
        if sys.platform != "darwin":
            raise InputError("密钥管理需要 macOS 钥匙串。")
        reference = uuid4().hex
        # 通过 stdin 传十六进制数据，密钥不进入进程参数、磁盘、shell 或日志。
        command = f"add-generic-password -s {self.service} -a {reference} -X {secret.encode().hex()}\n"
        try:
            result = subprocess.run(["/usr/bin/security", "-i"], input=command.encode(), capture_output=True, timeout=30)
            if result.returncode or not hmac.compare_digest(self.get(reference).encode(), secret.encode()):
                raise InputError("无法保存飞书密钥，请检查 macOS 钥匙串是否已解锁。")
        except (subprocess.SubprocessError, OSError):
            raise InputError("无法保存飞书密钥，请检查 macOS 钥匙串是否已解锁。") from None
        return reference

    def delete(self, reference: str):
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", reference],
                       capture_output=True, timeout=30)
