// Notes are stored as Markdown (board 3b) but edited formatted. These convert the subset the
// toolbar writes, both ways: paragraphs, bold, italic, links, bulleted and numbered lists, pipe tables.

const HTML_ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const escapeHtml = (text: string) => text.replace(/[&<>"]/g, char => HTML_ESCAPES[char]);

const SAFE_HREF = /^(https?:|mailto:)/i;
const BULLET = /^\s*[-*+]\s+/;
const NUMBER = /^\s*\d+[.)]\s+/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const PLACEHOLDER = /\uE000(\d+)\uE000/g;

const emphasis = (html: string) => html.replace(/\*\*(?=\S)(.+?)\*\*/g, '<strong>$1</strong>').replace(/\*(?=\S)(.+?)\*/g, '<em>$1</em>');

function inlineToHtml(source: string): string {
	// Escapes and links are held aside so the emphasis pass cannot reach into them.
	const held: string[] = [];
	const hold = (html: string) => `\uE000${held.push(html) - 1}\uE000`;
	let text = source.replace(/\\([\\`*_{}[\]()#+\-.!|])/g, (_match, char: string) => hold(escapeHtml(char)));
	text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_match, label: string, href: string) => {
		const inner = emphasis(escapeHtml(label));
		return hold(SAFE_HREF.test(href) ? `<a href="${escapeHtml(href)}">${inner}</a>` : inner);
	});
	const restore = (html: string): string => html.replace(PLACEHOLDER, (_match, index: string) => restore(held[Number(index)]));
	return restore(emphasis(escapeHtml(text)));
}

// An escaped pipe is set aside so it does not split the cell.
const splitCells = (line: string) =>
	line
		.trim()
		.replace(/\\\|/g, '\uE001')
		.replace(/^\|/, '')
		.replace(/\|$/, '')
		.split('|')
		.map(cell => cell.replace(/\uE001/g, '\\|').trim());

const startsTable = (lines: string[], index: number) => TABLE_ROW.test(lines[index]) && TABLE_RULE.test(lines[index + 1] ?? '');
const startsBlock = (lines: string[], index: number) => BULLET.test(lines[index]) || NUMBER.test(lines[index]) || startsTable(lines, index);
// An empty cell needs a <br> or the caret cannot enter it.
const cellHtml = (cell: string) => inlineToHtml(cell) || '<br>';

/** Markdown in the toolbar's subset as HTML for the editor. Anything else stays as text. */
export function markdownToHtml(markdown: string): string {
	const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
	const blocks: string[] = [];
	let index = 0;
	while (index < lines.length) {
		const line = lines[index];
		if (!line.trim()) {
			index += 1;
		} else if (BULLET.test(line) || NUMBER.test(line)) {
			const marker = BULLET.test(line) ? BULLET : NUMBER;
			const items: string[] = [];
			while (index < lines.length && marker.test(lines[index])) {
				items.push(`<li>${inlineToHtml(lines[index].replace(marker, ''))}</li>`);
				index += 1;
			}
			const tag = marker === BULLET ? 'ul' : 'ol';
			blocks.push(`<${tag}>${items.join('')}</${tag}>`);
		} else if (startsTable(lines, index)) {
			const head = splitCells(line);
			const rows: string[][] = [];
			index += 2;
			while (index < lines.length && TABLE_ROW.test(lines[index])) {
				rows.push(splitCells(lines[index]));
				index += 1;
			}
			const headHtml = head.map(cell => `<th>${cellHtml(cell)}</th>`).join('');
			const bodyHtml = rows.map(row => `<tr>${head.map((_cell, column) => `<td>${cellHtml(row[column] ?? '')}</td>`).join('')}</tr>`);
			blocks.push(`<table><thead><tr>${headHtml}</tr></thead><tbody>${bodyHtml.join('')}</tbody></table>`);
		} else {
			const paragraph: string[] = [];
			while (index < lines.length && lines[index].trim() && !startsBlock(lines, index)) {
				paragraph.push(inlineToHtml(lines[index].trim()));
				index += 1;
			}
			blocks.push(`<p>${paragraph.join('<br>')}</p>`);
		}
	}
	return blocks.join('');
}

const BLOCK_TAGS = new Set(['P', 'DIV', 'UL', 'OL', 'TABLE', 'BLOCKQUOTE', 'PRE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6']);
const isElement = (node: Node): node is HTMLElement => node.nodeType === 1;
const isBlock = (node: Node) => isElement(node) && BLOCK_TAGS.has(node.tagName);
const oneLine = (text: string) => text.replace(/\s*\n\s*/g, ' ').trim();

// Keeps markers tight to the words: `**bold** text`, never `**bold **text`.
function wrap(text: string, marker: string): string {
	const [, lead, body, trail] = /^(\s*)([\s\S]*?)(\s*)$/.exec(text) ?? ['', '', text, ''];
	return body ? `${lead}${marker}${body}${marker}${trail}` : text;
}

function inlineToMarkdown(node: Node, inTable = false): string {
	if (node.nodeType === 3) {
		const text = (node.textContent ?? '').replace(/\u00a0/g, ' ').replace(/([\\*[\]])/g, '\\$1');
		return inTable ? text.replace(/\|/g, '\\|') : text;
	}
	if (!isElement(node)) {
		return '';
	}
	const inner = [...node.childNodes].map(child => inlineToMarkdown(child, inTable)).join('');
	const { fontWeight, fontStyle } = node.style;
	switch (node.tagName) {
		case 'BR':
			return '\n';
		case 'STRONG':
		case 'B':
			return wrap(inner, '**');
		case 'EM':
		case 'I':
			return wrap(inner, '*');
		case 'A': {
			const href = node.getAttribute('href') ?? '';
			return href && inner.trim() ? `[${inner}](${href})` : inner;
		}
		default:
			// Some browsers format with styled spans instead of tags.
			if (fontWeight === 'bold' || Number(fontWeight) >= 600) {
				return wrap(inner, '**');
			}
			return fontStyle === 'italic' ? wrap(inner, '*') : inner;
	}
}

// A line that would read as a list or table on the way back gets its marker escaped.
const paragraphToMarkdown = (text: string) =>
	text
		.split('\n')
		.map(line =>
			line
				.trim()
				.replace(/^([-+|])/, '\\$1')
				.replace(/^(\d+)([.)])(?=\s)/, '$1\\$2')
		)
		.join('\n')
		.trim();

function tableToMarkdown(table: Element): string {
	const rows = [...table.querySelectorAll('tr')].map(row =>
		[...row.children].filter(cell => cell.tagName === 'TD' || cell.tagName === 'TH').map(cell => oneLine(inlineToMarkdown(cell, true)))
	);
	if (!rows.length) {
		return '';
	}
	const width = Math.max(...rows.map(row => row.length));
	const line = (cells: string[]) => `| ${Array.from({ length: width }, (_cell, column) => cells[column] ?? '').join(' | ')} |`;
	return [line(rows[0]), line(Array<string>(width).fill('---')), ...rows.slice(1).map(line)].join('\n');
}

function blockToMarkdown(element: HTMLElement): string {
	if (element.tagName === 'UL' || element.tagName === 'OL') {
		const items = [...element.children].filter(child => child.tagName === 'LI');
		const marker = (index: number) => (element.tagName === 'OL' ? `${index + 1}.` : '-');
		return items.map((item, index) => `${marker(index)} ${oneLine(inlineToMarkdown(item))}`).join('\n');
	}
	if (element.tagName === 'TABLE') {
		return tableToMarkdown(element);
	}
	return paragraphToMarkdown(inlineToMarkdown(element));
}

/** The editor's content as Markdown, the inverse of `markdownToHtml`. */
export function htmlToMarkdown(root: HTMLElement): string {
	const blocks: string[] = [];
	let run = '';
	const flush = () => {
		blocks.push(paragraphToMarkdown(run));
		run = '';
	};
	root.childNodes.forEach(child => {
		if (isBlock(child)) {
			flush();
			// Editors wrap blocks in a div on Enter or paste; walk into it.
			const wrapsBlocks = [...child.childNodes].some(isBlock);
			blocks.push(wrapsBlocks ? htmlToMarkdown(child as HTMLElement) : blockToMarkdown(child as HTMLElement));
		} else {
			run += inlineToMarkdown(child);
		}
	});
	flush();
	return blocks.filter(Boolean).join('\n\n');
}

/** The first table in pasted HTML, rebuilt from its cell text; null when there is none. */
export function pastedTableHtml(html: string): string | null {
	if (!/<table[\s>]/i.test(html)) {
		return null;
	}
	const table = new DOMParser().parseFromString(html, 'text/html').querySelector('table');
	const markdown = table ? tableToMarkdown(table) : '';
	return markdown ? markdownToHtml(markdown) : null;
}
