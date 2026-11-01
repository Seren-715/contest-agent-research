"""Render model-authored Survey paragraphs and their explicit source IDs.

Ported from the team version that scored C 23.5/25 on the platform (eval
2026-10-26). Only presentation is assembled here: all claims and table cells
come from the model; citation IDs are validated against the supplied
materials. A plain Markdown response (not JSON) is passed through unchanged.
"""
import json

from handlers.base import HandlerError
from handlers.evidence import CITATION_RE


def _close_mismatched_containers(raw):
    """Insert only omitted container closers before an existing parent closer.

    Never alter strings, values, keys or commas, and never complete a truncated
    response. The result must still pass JSON parsing and the Survey schema.
    """
    stack, output = [], []
    quoted = escaped = False
    inserted = 0
    closing = {'{': '}', '[': ']'}
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in closing:
            stack.append(char)
        elif char in '}]':
            opening = '{' if char == '}' else '['
            if opening not in stack:
                raise ValueError('unexpected closing container')
            while stack[-1] != opening:
                output.append(closing[stack.pop()])
                inserted += 1
                if inserted > 8:
                    raise ValueError('too many missing container closers')
            stack.pop()
        output.append(char)
    if quoted or stack:
        raise ValueError('truncated Survey response cannot be completed by the formatter')
    return ''.join(output), inserted


def render_survey(raw, records, diagnostics=None):
    if not raw.lstrip().startswith('{'):
        return raw  # Compatible with a complete Markdown model response.
    try:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            closed, count = _close_mismatched_containers(raw)
            document = json.loads(closed)
            if diagnostics is not None:
                diagnostics['inserted_container_closers'] = count
        sections = document['sections']
        comparison = document.get('comparison', [])
        if not isinstance(sections, list) or not sections:
            raise ValueError('sections must be a nonempty list')
        if not isinstance(comparison, list):
            raise ValueError('comparison must be a list')

        def prose(value):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('each text field must be a nonempty string')
            if CITATION_RE.search(value):
                raise ValueError('text fields must not repeat bracket citations; put IDs in citations')
            return value.strip()

        def citation(values):
            if not isinstance(values, list) or not values:
                raise ValueError('each paragraph requires model-selected citations')
            if any(type(value) is not int or value < 1 or value > len(records) for value in values):
                raise ValueError('citation IDs must point to supplied papers')
            return '[' + ','.join(str(value) for value in sorted(set(values))) + ']'

        lines = []
        for section in sections:
            heading = prose(section['heading'])
            if '\n' in heading or '参考文献' in heading:
                raise ValueError('invalid section heading')
            lines.extend(['## ' + heading.lstrip('# '), ''])
            paragraphs = section['paragraphs']
            if not isinstance(paragraphs, list) or not paragraphs:
                raise ValueError('section paragraphs must be a nonempty list')
            for paragraph in paragraphs:
                lines.extend([prose(paragraph['text']) + ' ' + citation(paragraph['citations']), ''])
        if comparison:
            lines.extend(['## 逐篇证据比较', '',
                          '| 文献 | 问题 | 方法 | 数据 | 指标 | 结论 | 局限 |',
                          '|---|---|---|---|---|---|---|'])
            for row in comparison:
                cells = [prose(row[key]).replace('|', '\\|').replace('\n', ' ')
                         for key in ('problem', 'method', 'data', 'metrics', 'findings', 'limitations')]
                lines.append('| ' + ' | '.join([citation([row['paper']])] + cells) + ' |')
            lines.append('')
        lines.extend(['## 参考文献', ''])
        lines.extend(f'[{index}] {record.filename}' for index, record in enumerate(records, 1))
        return '\n'.join(lines) + '\n'
    except (KeyError, TypeError, ValueError) as exc:
        raise HandlerError('Survey structure error: ' + str(exc)) from exc
