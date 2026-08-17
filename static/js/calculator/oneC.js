/**
 * The «1С, ч» field: a number or a small arithmetic expression.
 *
 * A hand-written tokenizer and recursive-descent parser over exactly the
 * grammar `calculator/expressions.py` accepts — numbers with a comma or a
 * dot, `+ - * /`, parentheses and whitespace. `eval()` and `new Function()`
 * are deliberately absent: this string comes from a text input.
 *
 * What this produces is a preview. The value that reaches the database is the
 * one the server parsed from the same expression.
 */
(function () {
  var api = window.windingCalculator = window.windingCalculator || {};
  var TOKEN = /^\s*(?:(\d+(?:[.,]\d+)?|[.,]\d+)|([-+*/()]))/;

  function tokenize(text) {
    var tokens = [], rest = text;
    while (rest.length) {
      var match = TOKEN.exec(rest);
      if (!match) return null;
      tokens.push(match[1] ? { number: Number(match[1].replace(',', '.')) } : { op: match[2] });
      rest = rest.slice(match[0].length);
      if (!rest.trim()) rest = '';
    }
    return tokens;
  }

  function parse(tokens) {
    var position = 0;
    function peek() { return tokens[position]; }
    function takeOperator(operators) {
      var token = peek();
      if (token && token.op && operators.indexOf(token.op) !== -1) { position += 1; return token.op; }
      return null;
    }
    function factor() {
      var sign = takeOperator('+-');
      if (sign) { var inner = factor(); return inner === null ? null : (sign === '-' ? -inner : inner); }
      var token = peek();
      if (token && typeof token.number === 'number') { position += 1; return token.number; }
      if (token && token.op === '(') {
        position += 1;
        var value = expression();
        if (value === null || takeOperator(')') === null) return null;
        return value;
      }
      return null;
    }
    function term() {
      var value = factor();
      while (value !== null) {
        var operator = takeOperator('*/');
        if (!operator) return value;
        var right = factor();
        if (right === null) return null;
        if (operator === '/' && right === 0) return null;
        value = operator === '*' ? value * right : value / right;
      }
      return null;
    }
    function expression() {
      var value = term();
      while (value !== null) {
        var operator = takeOperator('+-');
        if (!operator) return value;
        var right = term();
        if (right === null) return null;
        value = operator === '+' ? value + right : value - right;
      }
      return null;
    }
    var result = expression();
    return result !== null && position === tokens.length ? result : null;
  }

  /** The evaluated hours, `null` for an empty field, `NaN` for bad input. */
  api.evaluateOneC = function (raw) {
    var text = String(raw == null ? '' : raw).trim();
    if (!text) return null;
    var tokens = tokenize(text);
    if (!tokens) return NaN;
    var value = parse(tokens);
    if (value === null || !Number.isFinite(value) || value < 0) return NaN;
    return value;
  };
})();
