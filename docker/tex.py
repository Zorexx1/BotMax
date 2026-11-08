# MAX не показывает LaTeX, а GigaChat всё равно иногда пишет $\Phi_B = LI$ и \frac{a}{b}.
# Переводим в обычный текст: Φ_B = LI, a/b, x².
import re

GREEK = {
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'Gamma': 'Γ', 'delta': 'δ', 'Delta': 'Δ', 'epsilon': 'ε',
    'varepsilon': 'ε', 'zeta': 'ζ', 'eta': 'η', 'theta': 'θ', 'vartheta': 'θ', 'Theta': 'Θ', 'iota': 'ι',
    'kappa': 'κ', 'lambda': 'λ', 'Lambda': 'Λ', 'mu': 'μ', 'nu': 'ν', 'xi': 'ξ', 'Xi': 'Ξ', 'pi': 'π',
    'Pi': 'Π', 'rho': 'ρ', 'sigma': 'σ', 'Sigma': 'Σ', 'tau': 'τ', 'upsilon': 'υ', 'phi': 'φ', 'varphi': 'φ',
    'Phi': 'Φ', 'chi': 'χ', 'psi': 'ψ', 'Psi': 'Ψ', 'omega': 'ω', 'Omega': 'Ω',
}
SYMBOLS = {
    'cdot': '·', 'times': '×', 'div': '÷', 'pm': '±', 'mp': '∓', 'leq': '≤', 'le': '≤', 'geq': '≥',
    'ge': '≥', 'neq': '≠', 'ne': '≠', 'approx': '≈', 'sim': '~', 'equiv': '≡', 'infty': '∞', 'to': '→',
    'rightarrow': '→', 'Rightarrow': '⇒', 'leftarrow': '←', 'Leftrightarrow': '⇔', 'degree': '°',
    'circ': '°', 'partial': '∂', 'nabla': '∇', 'sum': 'Σ', 'prod': 'Π', 'int': '∫', 'angle': '∠',
    'perp': '⊥', 'parallel': '∥', 'propto': '∝', 'ldots': '…', 'dots': '…', 'cdots': '⋯', 'in': '∈',
    'cup': '∪', 'cap': '∩', 'emptyset': '∅', 'forall': '∀', 'exists': '∃', 'log': 'log', 'ln': 'ln',
    'lg': 'lg', 'sin': 'sin', 'cos': 'cos', 'tg': 'tg', 'tan': 'tg', 'ctg': 'ctg', 'cot': 'ctg',
    'lim': 'lim', 'max': 'max', 'min': 'min', 'quad': ' ', 'qquad': ' ', 'percent': '%',
}
SUP = dict(zip('0123456789+-−=(),nixabcdefghjklmoprstuvwyz', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾,ⁿⁱˣᵃᵇᶜᵈᵉᶠᵍʰʲᵏˡᵐᵒᵖʳˢᵗᵘᵛʷʸᶻ'))
SUB = dict(zip('0123456789+-=(),aehijklmnoprstuvx', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎,ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ'))
WRAPPERS = ('text', 'mathrm', 'mathbf', 'mathit', 'operatorname', 'textbf', 'mbox', 'boldsymbol')


def _group(s, i):
    while i < len(s) and s[i] == ' ':
        i += 1
    if i >= len(s):
        return '', i
    if s[i] != '{':
        if s[i] == '\\':
            m = re.match(r'\\[A-Za-z]+', s[i:])
            if m:
                return m.group(0), i + len(m.group(0))
        return s[i], i + 1
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                return s[i + 1:j], j + 1
    return s[i + 1:], len(s)


def _wrap(part, strict=False):
    part = part.strip()
    if re.search(r'[\s+\-−·×/±]', part):
        return f'({part})'
    # x/2a читается двусмысленно, пишем x/(2a)
    if strict and len(part) > 1 and re.search(r'\d', part) and re.search(r'[^\d.,]', part):
        return f'({part})'
    return part


def _script(content, table, mark):
    content = content.strip()
    if content and all(ch in table for ch in content):
        return ''.join(table[ch] for ch in content)
    return mark + (f'({content})' if re.search(r'[\s+\-−·×/=]', content) else content)


def _command(m):
    name, space = m.group(1), m.group(2)
    if name in GREEK:
        return GREEK[name]  # \Delta t - это Δt, пробел не нужен
    return SYMBOLS.get(name, name) + space


def convert(tex):
    s = tex
    for _ in range(20):  # вложенные \frac и \sqrt раскрываем по одному
        m = re.search(r'\\(?:d|t)?frac|\\sqrt|\\(?:' + '|'.join(WRAPPERS) + r')\b|\\vec|\\overline|\\bar|\\hat|\\mathcal', s)
        if not m:
            break
        name, pos = m.group(0)[1:], m.end()
        if name.endswith('frac'):
            top, pos = _group(s, pos)
            bottom, pos = _group(s, pos)
            s = s[:m.start()] + f'{_wrap(convert(top))}/{_wrap(convert(bottom), strict=True)}' + s[pos:]
        elif name == 'sqrt':
            degree = ''
            if pos < len(s) and s[pos] == '[':
                end = s.find(']', pos)
                degree, pos = s[pos + 1:end], end + 1
            inner, pos = _group(s, pos)
            inner = convert(inner)
            body = f'({inner})' if len(inner) > 1 and not inner.replace('.', '').replace(',', '').isdigit() else inner
            root = (''.join(SUP.get(c, c) for c in degree) if degree else '') + '√' + body
            s = s[:m.start()] + root + s[pos:]
        else:
            inner, pos = _group(s, pos)
            inner = convert(inner)
            if name == 'vec':
                inner += '⃗'
            elif name in ('overline', 'bar'):
                inner += '̄'
            elif name == 'hat':
                inner += '̂'
            elif name == 'mathcal' and inner == 'E':
                inner = 'ℰ'
            s = s[:m.start()] + inner + s[pos:]
    s = s.replace('\\\\', ' ')
    s = re.sub(r'\^\s*\{?\\circ\}?', '°', s)
    s = re.sub(r'\\left|\\right|\\displaystyle|\\limits', '', s)
    s = re.sub(r'\\[,;:!> ]', ' ', s)
    s = s.replace('\\{', '{').replace('\\}', '}').replace('\\%', '%').replace('\\$', '$')
    s = re.sub(r'\\([A-Za-z]+)( ?)', _command, s)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch in '^_' and i + 1 < len(s):
            content, i = _group(s, i + 1)
            out.append(_script(convert(content), SUP if ch == '^' else SUB, ch))
            continue
        out.append(ch)
        i += 1
    s = ''.join(out).replace('{', '').replace('}', '')
    return re.sub(r'[ \t]{2,}', ' ', s).strip()


def to_text(text):
    if '\\' not in text and '$' not in text:
        return text
    text = text.replace('\\%', '%')
    text = re.sub(r'\$\$(.+?)\$\$|\\\[(.+?)\\\]', lambda m: convert(m.group(1) or m.group(2)), text, flags=re.S)
    text = re.sub(r'\\\((.+?)\\\)', lambda m: convert(m.group(1)), text, flags=re.S)
    text = re.sub(r'(?<![\d\w])\$([^$\n]+?)\$', lambda m: convert(m.group(1)), text)
    if re.search(r'\\[A-Za-z]+', text):
        text = re.sub(r'\\[A-Za-z]+(?:\{[^{}]*\}){0,2}', lambda m: convert(m.group(0)), text)
    return text
