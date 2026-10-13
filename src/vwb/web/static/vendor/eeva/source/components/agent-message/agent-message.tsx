import { Fragment } from 'react';

import { cn } from '@/lib/utils';

import { TextLink } from '../text-link/text-link';

export interface AgentTableColumn {
	label: string;
	/** A grid track: "60px", "110px". Defaults to 1fr. */
	width?: string;
	/** Tertiary text, for a descriptor column like Visual (I1). */
	muted?: boolean;
}

/** Text, items and cells may carry **bold**, [label](url) and bare http(s) addresses, rendered as #1053 did. */
export type AgentMessageBlock =
	| { type: 'text'; text: string }
	| { type: 'bullets'; items: string[] }
	| { type: 'numbered'; items: string[] }
	| { type: 'table'; columns: AgentTableColumn[]; rows: string[][] };

export interface AgentMessageProps {
	/** Paragraphs, bullet lists and tables, in order. */
	blocks?: AgentMessageBlock[];
	/** Shorthand for a single paragraph. */
	children?: React.ReactNode;
	/** Buttons 10px under the message, like Try again and Finish with what we have after a failure (G2). */
	actions?: React.ReactNode;
	/** "4:24 PM", top right, shown only on hover or focus (as #1053). */
	time?: string;
	className?: string;
}

// Space above a block that follows another (E4): 4px over a list, 6px over a table, 10px over text.
const BLOCK_GAP: Record<AgentMessageBlock['type'], string> = { text: 'mt-2.5', bullets: 'mt-1', numbered: 'mt-1', table: 'mt-1.5' };

// **bold**, [label](url), or a bare address that stops before trailing punctuation.
const INLINE = /(\*\*[^*]+\*\*|\[[^\]]+\]\([^)\s]+\)|https?:\/\/[^\s<>()]*[^\s<>().,;:!?'"])/g;
const SAFE_HREF = /^https?:\/\//i;

function Link({ href, children }: { href: string; children: React.ReactNode }) {
	// Only web addresses become links; anything else (javascript:, mailto:) reads as its label.
	if (!SAFE_HREF.test(href)) {
		return <span>{children}</span>;
	}
	return (
		<TextLink className='text-[length:inherit] leading-[inherit]' href={href} external>
			{children}
		</TextLink>
	);
}

/** One line of agent copy: bold, links and bare addresses; everything else is text. */
function Inline({ text }: { text: string }) {
	return (
		<>
			{text.split(INLINE).map((part, index) => {
				if (index % 2 === 0) {
					return part ? <Fragment key={index}>{part}</Fragment> : null;
				}
				if (part.startsWith('**')) {
					return (
						<strong key={index} className='font-semibold'>
							<Inline text={part.slice(2, -2)} />
						</strong>
					);
				}
				const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(part);
				return link ? (
					<Link key={index} href={link[2]}>
						{link[1]}
					</Link>
				) : (
					<Link key={index} href={part}>
						{part}
					</Link>
				);
			})}
		</>
	);
}

// Head and cells keep the message's 24px lines, as I1 draws them: 31px head, 39px rows.
function Table({ columns, rows }: { columns: AgentTableColumn[]; rows: string[][] }) {
	const tracks = { gridTemplateColumns: columns.map(column => column.width ?? '1fr').join(' ') };
	return (
		<div className='mb-0.5' role='table'>
			<div
				className='grid items-center gap-3 border-b border-[color:var(--ds-table-head-rule)] pb-1.5 text-[length:var(--ds-type-table-head-size)] font-semibold uppercase leading-[var(--ds-type-body-leading)] tracking-[var(--ds-type-table-head-tracking)] text-[var(--ds-text-muted)]'
				role='row'
				style={tracks}
			>
				{columns.map(column => (
					<span key={column.label} role='columnheader'>
						{column.label}
					</span>
				))}
			</div>
			{rows.map((row, rowIndex) => (
				<div
					key={rowIndex}
					className='grid items-center gap-3 border-b border-[color:var(--ds-border-divider)] py-[7px] text-[length:var(--ds-type-control-size)] leading-[var(--ds-type-body-leading)]'
					role='row'
					style={tracks}
				>
					{row.map((cell, cellIndex) => (
						<span key={cellIndex} className={cn(columns[cellIndex]?.muted && 'text-[var(--ds-text-tertiary)]')} role='cell'>
							<Inline text={cell} />
						</span>
					))}
				</div>
			))}
		</div>
	);
}

/**
 * The agent's words (Eeva V1 board 4, from A2, C2, I1): plain 15/24 in
 * primary text, no bubble, no avatar. Bullets sit 4px under their lead-in
 * at a 20px indent; a table is a small-caps header over hairline rows.
 * Bold, links and bare addresses render inline; the time shows on hover.
 *
 * @status   draft
 * @variants plain; bullets; numbered; table; actions; inline bold and links; hover time
 * @use      Every agent reply in the feed, built from blocks (`toAgentBlocks` reads the agent's Markdown into them).
 * @avoid    Never a bubble. A table here is for reading; it is never stored as a table in the panel.
 */
export function AgentMessage({ blocks, children, actions, time, className }: AgentMessageProps) {
	return (
		<div
			className={cn(
				'group/agent relative text-[length:var(--ds-type-body-size)] leading-[var(--ds-type-body-leading)] text-[var(--ds-text-primary)]',
				// Room for the hover time, so it never sits on the first line's words.
				time && 'pr-14',
				// Board 8 rise in: base 220, standard, 6px up, on arrival.
				'animate-in fade-in-0 slide-in-from-bottom-[var(--ds-distance-rise)] [animation-duration:var(--ds-dur-base)] [animation-timing-function:var(--ds-ease-standard)] motion-reduce:animate-none',
				className
			)}
		>
			{children ? <p className='m-0'>{children}</p> : null}
			{blocks?.map((block, index) => {
				const gap = index > 0 || children ? BLOCK_GAP[block.type] : undefined;
				if (block.type === 'bullets' || block.type === 'numbered') {
					const List = block.type === 'bullets' ? 'ul' : 'ol';
					return (
						<List key={index} className={cn('m-0 pl-5', block.type === 'bullets' ? 'list-disc' : 'list-decimal', gap)}>
							{block.items.map((item, itemIndex) => (
								<li key={itemIndex}>
									<Inline text={item} />
								</li>
							))}
						</List>
					);
				}
				if (block.type === 'table') {
					return (
						<div key={index} className={gap}>
							<Table columns={block.columns} rows={block.rows} />
						</div>
					);
				}
				return (
					<p key={index} className={cn('m-0', gap)}>
						<Inline text={block.text} />
					</p>
				);
			})}
			{actions ? <div className='mt-2.5 flex gap-2'>{actions}</div> : null}
			{time ? (
				<span className='pointer-events-none absolute right-0 top-0 text-[length:var(--ds-type-caption-size)] leading-[var(--ds-type-caption-leading)] text-[var(--ds-text-muted)] opacity-0 transition-opacity [transition-duration:var(--ds-dur-fast)] group-focus-within/agent:opacity-100 group-hover/agent:opacity-100'>
					{time}
				</span>
			) : null}
		</div>
	);
}
