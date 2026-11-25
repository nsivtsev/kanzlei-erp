// A small Markdown subset rendered with DOM nodes only. Model output is never parsed as HTML.

export type MarkerFactory = (number: number) => Node | null;

const INLINE = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_|`[^`\n]+`|\[\d{1,3}\])/;

function inline(parent: Node, text: string, marker: MarkerFactory) {
  for (const part of text.split(INLINE)) {
    if (!part) continue;
    let node: Node;
    if ((part.startsWith('**') && part.endsWith('**') && part.length > 4)
        || (part.startsWith('__') && part.endsWith('__') && part.length > 4)) {
      node = document.createElement('strong');
      inline(node, part.slice(2, -2), marker);
    } else if (part.length > 2 && ((part.startsWith('*') && part.endsWith('*'))
        || (part.startsWith('_') && part.endsWith('_')))) {
      node = document.createElement('em');
      inline(node, part.slice(1, -1), marker);
    } else if (part.length > 2 && part.startsWith('`') && part.endsWith('`')) {
      node = document.createElement('code');
      node.textContent = part.slice(1, -1);
    } else if (/^\[\d{1,3}\]$/.test(part)) {
      node = marker(Number(part.slice(1, -1))) ?? document.createTextNode(part);
    } else {
      node = document.createTextNode(part);
    }
    parent.appendChild(node);
  }
}

function lines(parent: HTMLElement, rows: string[], marker: MarkerFactory) {
  rows.forEach((row, index) => {
    if (index) parent.appendChild(document.createElement('br'));
    inline(parent, row, marker);
  });
}

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d{1,3}[.)]\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*)$/;

export function renderMarkdown(text: string, marker: MarkerFactory = () => null): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const source = text.replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  while (index < source.length) {
    const line = source[index];
    if (!line.trim()) {
      index++;
      continue;
    }
    if (line.trimStart().startsWith('```')) {
      const code: string[] = [];
      index++;
      while (index < source.length && !source[index].trimStart().startsWith('```')) code.push(source[index++]);
      index++;
      const pre = document.createElement('pre');
      const element = document.createElement('code');
      element.textContent = code.join('\n');
      pre.appendChild(element);
      fragment.appendChild(pre);
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      // Answers sit under the page heading; keep their headings small.
      const element = document.createElement(heading[1].length <= 2 ? 'h3' : 'h4');
      inline(element, heading[2], marker);
      fragment.appendChild(element);
      index++;
      continue;
    }
    const kind = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (kind) {
      const list = document.createElement(kind === BULLET ? 'ul' : 'ol');
      while (index < source.length && kind.test(source[index])) {
        const item = document.createElement('li');
        inline(item, kind.exec(source[index])![1], marker);
        list.appendChild(item);
        index++;
      }
      fragment.appendChild(list);
      continue;
    }
    const paragraph: string[] = [];
    while (index < source.length && source[index].trim() && !HEADING.test(source[index])
        && !BULLET.test(source[index]) && !NUMBERED.test(source[index])
        && !source[index].trimStart().startsWith('```')) {
      paragraph.push(source[index++]);
    }
    const element = document.createElement('p');
    lines(element, paragraph, marker);
    fragment.appendChild(element);
  }
  return fragment;
}
