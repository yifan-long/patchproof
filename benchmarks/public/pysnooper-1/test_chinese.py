# -*- coding: utf-8 -*-
# Copyright 2019 Ram Rachum and collaborators.
# This program is distributed under the MIT license.
# Reconstructed official test for PySnooper issue #124 (Chinese source
# handling). Self-contained: executes a source file without an explicit
# PEP-263 coding declaration under a loader-less __main__ context so that
# pysnooper/tracer.get_source_from_frame must read and decode the file
# itself. Python 3 source files default to UTF-8, so the snoop output must
# preserve the Chinese comment byte-for-byte.

import builtins
import os
import tempfile

import pysnooper


def _chinese_snoop_output():
    folder = tempfile.mkdtemp(prefix="pysnooper-chinese-")
    script_path = os.path.join(folder, "chinese_no_decl.py")
    log_path = os.path.join(folder, "snoop.log")
    inner = (
        "import pysnooper\n"
        "@pysnooper.snoop(%r)\n"
        "def foo():\n"
        "    x = 'abc'  # 中文注释\n"
        "    return 7\n"
        "foo()\n"
    ) % log_path
    with open(script_path, "wb") as handle:
        handle.write(inner.encode("utf-8"))
    code = compile(open(script_path, "rb").read(), script_path, "exec")
    globs = {
        "__name__": "__main__",
        "__file__": script_path,
        "__loader__": None,
        "__builtins__": builtins,
    }
    exec(code, globs)
    with open(log_path, encoding="utf-8") as handle:
        return handle.read()


def test_chinese():
    output = _chinese_snoop_output()
    assert "# 中文注释" in output, output
