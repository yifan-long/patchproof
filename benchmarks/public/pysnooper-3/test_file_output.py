import os
import tempfile

import pysnooper


def test_file_output():
    folder = tempfile.mkdtemp(prefix="pysnooper")
    path = os.path.join(folder, "foo.log")

    @pysnooper.snoop(path)
    def my_function(foo):
        x = 7
        y = 8
        return x + y

    result = my_function("baba")
    assert result == 15
    with open(path, encoding="utf-8") as output_file:
        output = output_file.read()
    assert "x = 7" in output, output
    assert "y = 8" in output, output
