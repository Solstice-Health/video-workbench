import { cn } from '@/lib/utils';

import { Icon } from '../../icons';

export type TextLinkKind = 'inline' | 'url' | 'back';

const KIND_CLASS: Record<TextLinkKind, string> = {
	inline: 'font-medium underline underline-offset-[3px]',
	url: 'text-[var(--ds-text-accent)] hover:underline',
	back: 'inline-flex items-center gap-1.5 font-medium text-[var(--ds-text-secondary)] hover:text-[var(--ds-text-primary)]'
};

export interface TextLinkProps {
	href: string;
	kind?: TextLinkKind;
	/** Opens in a new tab. */
	external?: boolean;
	className?: string;
	children: React.ReactNode;
}

/**
 * A link (Eeva V1 board 3). Links navigate; buttons do things. Board 3 draws
 * rest only; hover is the smallest change that reads as a link.
 *
 * @status   draft
 * @variants kind: inline | url | back; external
 * @use      `inline`: an action inside a sentence, like View full chat history. `url`: a web address shown as itself. `back`: View all requests, above a page title.
 * @avoid    Never for an action that does not navigate. A standalone Exit is a `text` Button.
 */
export function TextLink({ href, kind = 'inline', external = false, className, children }: TextLinkProps) {
	return (
		<a
			className={cn(
				'rounded-[var(--ds-radius-xs)] text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] outline-none focus-visible:[box-shadow:var(--ds-focus-ring)]',
				KIND_CLASS[kind],
				className
			)}
			href={href}
			rel={external ? 'noopener noreferrer' : undefined}
			target={external ? '_blank' : undefined}
		>
			{kind === 'back' ? <Icon name='arrow-left' size={14} /> : null}
			{children}
		</a>
	);
}
