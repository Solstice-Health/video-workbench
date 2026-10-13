'use client';

import { Fragment, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { cn } from '@/lib/utils';

import { Icon, type IconName } from '../../icons';
import { Skeleton } from '../skeleton/skeleton';
import { Tooltip } from '../tooltip/tooltip';
import { UrlField } from '../url-field/url-field';
import { htmlToMarkdown, markdownToHtml, pastedTableHtml } from './markdown';

type ToolId = 'bold' | 'italic' | 'bullets' | 'numbers' | 'link' | 'table';

const TOOLS: { id: ToolId; label: string; shortcut?: string; icon: IconName; divider?: boolean }[] = [
	{ id: 'bold', label: 'Bold', shortcut: '⌘B', icon: 'bold' },
	{ id: 'italic', label: 'Italic', shortcut: '⌘I', icon: 'italic' },
	{ id: 'bullets', label: 'Bulleted list', icon: 'list', divider: true },
	{ id: 'numbers', label: 'Numbered list', icon: 'list-ordered' },
	{ id: 'link', label: 'Link', shortcut: '⌘K', icon: 'link', divider: true },
	{ id: 'table', label: 'Table', icon: 'table' }
];

const COMMAND: Partial<Record<ToolId, string>> = {
	bold: 'bold',
	italic: 'italic',
	bullets: 'insertUnorderedList',
	numbers: 'insertOrderedList'
};

// A paragraph after the table so typing can carry on below it.
const NEW_TABLE = `${markdownToHtml('| Column | Column |\n| --- | --- |\n|  |  |')}<p><br></p>`;

// The frame's paragraph and list rhythm (I4); tables are not drawn, so they borrow the header band.
const CONTENT_CLASS =
	'flex-1 px-2.5 py-2 text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] text-[var(--ds-text-primary)] outline-none [&>*:last-child]:mb-0 [&_a]:text-[var(--ds-text-accent)] [&_a]:underline [&_ol]:mb-1.5 [&_ol]:list-decimal [&_ol]:pl-[18px] [&_p]:mb-1.5 [&>div]:mt-1.5 [&_table]:mb-1.5 [&_table]:w-full [&_table]:border-collapse [&_td]:border [&_td]:border-[var(--ds-border-default)] [&_td]:px-2 [&_td]:py-1 [&_th]:border [&_th]:border-[var(--ds-border-default)] [&_th]:bg-[var(--ds-surface-band)] [&_th]:px-2 [&_th]:py-1 [&_th]:text-left [&_th]:font-semibold [&_ul]:mb-1.5 [&_ul]:list-disc [&_ul]:pl-[18px]';

const exec = (command: string, value?: string) => document.execCommand(command, false, value);

function selectionIn(editor: HTMLElement, selector: string): Element | null {
	const node = window.getSelection()?.anchorNode;
	const element = node?.nodeType === 1 ? (node as Element) : node?.parentElement;
	const match = element?.closest(selector);
	return match && editor.contains(match) ? match : null;
}

const textOffset = (root: Node, node: Node, offset: number) => {
	const range = document.createRange();
	range.setStart(root, 0);
	range.setEnd(node, offset);
	return range.toString().length;
};

function pointAt(root: Node, target: number): [Node, number] {
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	let left = target;
	let last: [Node, number] = [root, 0];
	for (let node = walker.nextNode(); node; node = walker.nextNode()) {
		const length = node.textContent?.length ?? 0;
		if (left <= length) {
			return [node, left];
		}
		left -= length;
		last = [node, length];
	}
	return last;
}

// Chrome's list commands drop a collapsed caret to the start of the item; put it back by text position.
function keepCaret(editor: HTMLElement, command: () => void) {
	const selection = window.getSelection();
	const { anchorNode, anchorOffset, focusNode, focusOffset } = selection ?? {};
	if (!selection || !anchorNode || !focusNode || !editor.contains(anchorNode)) {
		command();
		return;
	}
	const anchor = textOffset(editor, anchorNode, anchorOffset ?? 0);
	const focus = textOffset(editor, focusNode, focusOffset ?? 0);
	command();
	const moved = selection.anchorNode && textOffset(editor, selection.anchorNode, selection.anchorOffset) !== anchor;
	if (moved) {
		selection.setBaseAndExtent(...pointAt(editor, anchor), ...pointAt(editor, focus));
	}
}

// A table cannot sit inside a paragraph or list item, and the browser moves a caret placed between
// blocks back into one, so the table goes in after the caret's top-level block by hand.
function insertTable(editor: HTMLElement) {
	let block = window.getSelection()?.anchorNode ?? null;
	while (block && block.parentNode !== editor) {
		block = block.parentNode;
	}
	const holder = document.createElement('div');
	holder.innerHTML = NEW_TABLE;
	const nodes = [...holder.childNodes];
	if (block) {
		(block as ChildNode).after(...nodes);
	} else {
		editor.append(...nodes);
	}
	// The first header cell is selected, ready to type over.
	const range = document.createRange();
	range.selectNodeContents((nodes[0] as Element).querySelector('th') ?? editor);
	window.getSelection()?.removeAllRanges();
	window.getSelection()?.addRange(range);
}

const withScheme = (url: string) => (/^(https?:|mailto:)/i.test(url) ? url : `https://${url}`);

export interface RichTextAreaProps {
	/** Markdown. */
	value: string;
	/** Markdown, on every edit. */
	onChange?: (markdown: string) => void;
	/** When focus leaves the field and its toolbar: a natural moment to save. */
	onBlur?: () => void;
	placeholder?: string;
	/** A Skeleton inside the field while the agent writes the notes (A1). */
	loading?: boolean;
	autoFocus?: boolean;
	id?: string;
	'aria-label'?: string;
	className?: string;
}

function RichTextEditor({
	value,
	onChange,
	onBlur,
	placeholder = 'Type a note',
	autoFocus = false,
	id,
	'aria-label': ariaLabel = 'Notes',
	className
}: Omit<RichTextAreaProps, 'loading'>) {
	const editorRef = useRef<HTMLDivElement>(null);
	// The Markdown this editor last wrote or was given; an echo of it must not reset the caret.
	const shown = useRef<string | null>(null);
	const savedRange = useRef<Range | null>(null);
	const [focused, setFocused] = useState(false);
	const [empty, setEmpty] = useState(!value.trim());
	const [active, setActive] = useState<Partial<Record<ToolId, boolean>>>({});
	const [linking, setLinking] = useState(false);

	useLayoutEffect(() => {
		const editor = editorRef.current;
		if (!editor || value === shown.current) {
			return;
		}
		editor.innerHTML = markdownToHtml(value);
		shown.current = value;
		setEmpty(!value.trim());
	}, [value]);

	useEffect(() => {
		const editor = editorRef.current;
		if (!autoFocus || !editor) {
			return;
		}
		editor.focus();
		window.getSelection()?.selectAllChildren(editor);
		window.getSelection()?.collapseToEnd();
	}, [autoFocus]);

	useEffect(() => {
		const editor = editorRef.current;
		if (!focused || !editor) {
			return undefined;
		}
		const refresh = () =>
			setActive({
				bold: document.queryCommandState('bold'),
				italic: document.queryCommandState('italic'),
				bullets: Boolean(selectionIn(editor, 'ul')),
				numbers: Boolean(selectionIn(editor, 'ol')),
				link: Boolean(selectionIn(editor, 'a')),
				table: Boolean(selectionIn(editor, 'table'))
			});
		refresh();
		document.addEventListener('selectionchange', refresh);
		return () => document.removeEventListener('selectionchange', refresh);
	}, [focused]);

	const emit = () => {
		const editor = editorRef.current;
		if (!editor) {
			return;
		}
		const markdown = htmlToMarkdown(editor);
		setEmpty(!markdown && !editor.querySelector('li, table'));
		if (markdown !== shown.current) {
			shown.current = markdown;
			onChange?.(markdown);
		}
	};

	const restoreSelection = () => {
		const editor = editorRef.current;
		const range = savedRange.current;
		editor?.focus();
		if (range) {
			window.getSelection()?.removeAllRanges();
			window.getSelection()?.addRange(range);
		}
		savedRange.current = null;
	};

	const startLink = () => {
		const editor = editorRef.current;
		const link = editor ? selectionIn(editor, 'a') : null;
		if (link) {
			// A second press on a link removes it.
			const range = document.createRange();
			range.selectNodeContents(link);
			window.getSelection()?.removeAllRanges();
			window.getSelection()?.addRange(range);
			exec('unlink');
			emit();
			return;
		}
		const selection = window.getSelection();
		savedRange.current = selection?.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
		setLinking(true);
	};

	const applyLink = (raw: string) => {
		setLinking(false);
		restoreSelection();
		const url = raw.trim();
		if (!url) {
			return;
		}
		const href = withScheme(url);
		if (window.getSelection()?.isCollapsed) {
			const anchor = document.createElement('a');
			anchor.href = href;
			anchor.textContent = url;
			exec('insertHTML', anchor.outerHTML);
		} else {
			exec('createLink', href);
		}
		emit();
	};

	const run = (tool: ToolId) => {
		if (tool === 'link') {
			startLink();
			return;
		}
		const editor = editorRef.current;
		// Refocusing an already focused editor can move its caret to the start.
		if (editor && document.activeElement !== editor) {
			editor.focus();
		}
		if (tool === 'table' && editor) {
			insertTable(editor);
		} else if (editor && (tool === 'bullets' || tool === 'numbers')) {
			keepCaret(editor, () => exec(COMMAND[tool] as string));
		} else {
			exec(COMMAND[tool] as string);
		}
		emit();
	};

	return (
		<div
			className={cn(
				'relative flex w-full flex-col overflow-hidden rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] transition-shadow [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)]',
				focused
					? 'min-h-24 [box-shadow:var(--ds-field-editing-rich)]'
					: cn('[box-shadow:var(--ds-field-rest)] hover:[box-shadow:var(--ds-field-hover)]', empty ? 'min-h-24' : 'min-h-14'),
				className
			)}
			onBlur={event => {
				if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
					setFocused(false);
					setLinking(false);
					onBlur?.();
				}
			}}
			onFocus={() => setFocused(true)}
		>
			{focused ? (
				<div
					aria-label='Formatting'
					className='flex items-center gap-0.5 border-b border-[var(--ds-rich-text-rule)] px-1.5 py-1 animate-in fade-in-0 [animation-duration:var(--ds-dur-fast)]'
					role='toolbar'
				>
					{TOOLS.map(tool => (
						<Fragment key={tool.id}>
							{tool.divider ? <span aria-hidden='true' className='mx-1 h-4 w-px bg-[var(--ds-rich-text-divider)]' /> : null}
							<Tooltip content={tool.label} shortcut={tool.shortcut}>
								<button
									aria-label={tool.label}
									aria-pressed={Boolean(active[tool.id])}
									className={cn(
										'flex size-[26px] items-center justify-center rounded-[var(--ds-radius-sm)] outline-none focus-visible:[box-shadow:var(--ds-focus-ring)]',
										active[tool.id]
											? 'bg-[var(--ds-rich-text-active)] text-[var(--ds-text-primary)]'
											: 'text-[var(--ds-text-secondary)] hover:bg-[var(--ds-action-icon-hover-tint)] hover:text-[var(--ds-text-primary)]'
									)}
									type='button'
									onClick={() => run(tool.id)}
									// Keeps the editor's selection while the button is pressed.
									onMouseDown={event => event.preventDefault()}
								>
									<Icon name={tool.icon} size={14} />
								</button>
							</Tooltip>
						</Fragment>
					))}
				</div>
			) : null}
			{linking ? (
				<div className='border-b border-[var(--ds-rich-text-rule)] px-1.5 py-1.5'>
					<UrlField
						aria-label='Link address'
						placeholder='Paste a link, then press Enter'
						autoFocus
						onKeyDown={event => {
							if (event.key === 'Enter') {
								event.preventDefault();
								applyLink(event.currentTarget.value);
							}
							if (event.key === 'Escape') {
								event.preventDefault();
								setLinking(false);
								restoreSelection();
							}
						}}
					/>
				</div>
			) : null}
			<div className='relative flex flex-1 flex-col'>
				{empty ? (
					<span
						aria-hidden='true'
						className='pointer-events-none absolute left-2.5 top-2 text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] text-[var(--ds-text-tertiary)]'
					>
						{placeholder}
					</span>
				) : null}
				<div
					ref={editorRef}
					aria-label={ariaLabel}
					aria-multiline='true'
					className={CONTENT_CLASS}
					id={id}
					role='textbox'
					tabIndex={0}
					contentEditable
					suppressContentEditableWarning
					onInput={emit}
					onKeyDown={event => {
						if (!(event.metaKey || event.ctrlKey)) {
							return;
						}
						const key = event.key.toLowerCase();
						const tool: ToolId | null = key === 'b' ? 'bold' : key === 'i' ? 'italic' : key === 'k' ? 'link' : null;
						// Underline has no Markdown, so the browser's own ⌘U is off.
						if (tool || key === 'u') {
							event.preventDefault();
						}
						if (tool) {
							run(tool);
						}
					}}
					onPaste={event => {
						event.preventDefault();
						const table = pastedTableHtml(event.clipboardData.getData('text/html'));
						if (table) {
							exec('insertHTML', table);
						} else {
							exec('insertText', event.clipboardData.getData('text/plain'));
						}
						emit();
					}}
				/>
			</div>
		</div>
	);
}

/**
 * The request's Notes (Eeva V1 board 3b, from I4). Edited formatted, stored as
 * Markdown; a toolbar with bold, italic, bulleted and numbered lists, link and
 * table appears on focus. Grows with its content.
 *
 * @status   draft
 * @variants loading; empty; filled; focused (toolbar)
 * @use      The Notes section of the request panel: anything the team should know that has no field.
 * @avoid    Not for a short value or a file's note (`TextField`). No headings, images or colours: only what the toolbar writes.
 */
export function RichTextArea({ loading = false, className, ...props }: RichTextAreaProps) {
	if (loading) {
		return (
			<div
				aria-busy='true'
				aria-label={props['aria-label'] ?? 'Notes'}
				className={cn(
					'flex min-h-24 w-full rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 py-2 [box-shadow:var(--ds-field-rest)]',
					className
				)}
				role='group'
			>
				<Skeleton shape='notes' />
			</div>
		);
	}
	return <RichTextEditor className={className} {...props} />;
}
