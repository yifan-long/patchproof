import io

import pysnooper


def test_custom_repr_single():
    string_io = io.StringIO()

    @pysnooper.snoop(string_io, custom_repr=(list, lambda l: "foofoo!"))
    def sum_to_x(x):
        l = list(range(x))
        return 7

    result = sum_to_x(10000)

    output = string_io.getvalue()
    assert result == 7
    assert "l = foofoo!" in output, output
