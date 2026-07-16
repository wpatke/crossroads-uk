"""Tiny SQL-literal helper shared by every transformer (spec §2 fidelity).

DuckDB cannot bind identifiers or file paths as query parameters, so a handful of
places must interpolate a string straight into SQL text -- a file path from the
install location, a boundary vintage label, a codebook variable name. None are
attacker-controlled, but "not attacker-controlled" is a different question from
"parses correctly": a perfectly friendly apostrophe in an install path (e.g.
C:\\Users\\Tom O'Brien\\...) turns a single-quoted SQL literal into a syntax error
and fails the whole build before it starts.

sql_str() is the single place that turns a Python string into a safe, single-quoted
SQL string literal by doubling any embedded single quote (the SQL-standard escape).
Use it for every interpolated string VALUE. It is NOT for identifiers (table/column
names) -- those follow a different escaping rule and are code-controlled here.
"""


def sql_str(value):
    """Return `value` as a safe, single-quoted SQL string literal.

    Doubles embedded single quotes: O'Brien -> 'O''Brien'. The surrounding quotes
    are part of the returned text, so call sites do NOT add their own quotes:
        f"... FROM read_csv({sql_str(path)}, ...) ..."
    """
    return "'" + str(value).replace("'", "''") + "'"
