"""A four-function arithmetic evaluator for the «1С, ч» field.

The field accepts either a plain number or the small expression a normer
would write down — `2,5*30`, `(2+3)*10`, `100/4` — and nothing else. Python's
`eval()`/`exec()` are not an option and never will be here: the string comes
from a browser, so it is tokenized and parsed by hand and only the grammar
below can be expressed at all.

    expression := term (('+' | '-') term)*
    term       := factor (('*' | '/') factor)*
    factor     := ('+' | '-') factor | '(' expression ')' | number
    number     := digits [('.' | ',') digits] | ('.' | ',') digits

The browser mirrors this in `static/js/calculator/oneC.js` for an instant
preview, but the stored number is always the one this module returned.
"""
import math
import re

MAX_LENGTH = 120

_TOKEN = re.compile(r'\s*(?:(\d+(?:[.,]\d+)?|[.,]\d+)|([-+*/()]))')


class OneCExpressionError(Exception):
    """The expression cannot be evaluated; the message is user-facing."""


def _tokenize(text):
    tokens, position = [], 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise OneCExpressionError('1С: допустимы только числа, + − × ÷ и скобки.')
        number, operator = match.groups()
        tokens.append(('number', float(number.replace(',', '.'))) if number else ('op', operator))
        position = match.end()
    return tokens


class _Parser:
    """Recursive descent over the token list; no lookahead beyond one token."""

    def __init__(self, tokens):
        self.tokens = tokens
        self.position = 0

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def take_operator(self, operators):
        kind, value = self.peek()
        if kind == 'op' and value in operators:
            self.position += 1
            return value
        return None

    def parse(self):
        value = self.expression()
        if self.position != len(self.tokens):
            raise OneCExpressionError('1С: некорректное выражение.')
        return value

    def expression(self):
        value = self.term()
        while True:
            operator = self.take_operator('+-')
            if operator is None:
                return value
            right = self.term()
            value = value + right if operator == '+' else value - right

    def term(self):
        value = self.factor()
        while True:
            operator = self.take_operator('*/')
            if operator is None:
                return value
            right = self.factor()
            if operator == '*':
                value *= right
            else:
                if right == 0:
                    raise OneCExpressionError('1С: деление на ноль.')
                value /= right
            if not math.isfinite(value):
                raise OneCExpressionError('1С: результат вне допустимого диапазона.')

    def factor(self):
        operator = self.take_operator('+-')
        if operator is not None:
            value = self.factor()
            return -value if operator == '-' else value
        kind, value = self.peek()
        if kind == 'number':
            self.position += 1
            return value
        if kind == 'op' and value == '(':
            self.position += 1
            inner = self.expression()
            if self.take_operator(')') is None:
                raise OneCExpressionError('1С: не закрыта скобка.')
            return inner
        raise OneCExpressionError('1С: некорректное выражение.')


def evaluate_one_c(raw):
    """Return `(expression, hours)` for the field, or `('', None)` if empty.

    The field is optional, so blank input is a normal answer rather than an
    error. Anything else must parse, stay finite and end up non-negative — a
    negative norm is a typo, not a value worth storing.
    """
    if raw is None:
        return '', None
    expression = str(raw).strip()
    if not expression:
        return '', None
    if len(expression) > MAX_LENGTH:
        raise OneCExpressionError(f'1С: выражение длиннее {MAX_LENGTH} символов.')
    value = _Parser(_tokenize(expression)).parse()
    if not math.isfinite(value):
        raise OneCExpressionError('1С: результат вне допустимого диапазона.')
    if value < 0:
        raise OneCExpressionError('1С: время не может быть отрицательным.')
    return expression, value
