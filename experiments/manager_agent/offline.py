"""Disable trace export and reject external Python socket connections."""

import ipaddress
import os
import socket
from contextlib import contextmanager
from unittest.mock import patch

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"


@contextmanager
def offline():
    from agents import set_tracing_disabled

    set_tracing_disabled(True)
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check_address(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            if not ipaddress.ip_address(address[0]).is_loopback:
                raise AssertionError("The experiment may only open loopback sockets.")

    def connect(sock, address):
        check_address(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(sock, address)
        return original_connect_ex(sock, address)

    with patch.object(socket.socket, "connect", connect):
        with patch.object(socket.socket, "connect_ex", connect_ex):
            yield
