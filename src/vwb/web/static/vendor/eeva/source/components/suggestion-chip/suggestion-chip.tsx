'use client';

import { cn } from '@/lib/utils';

import { Icon, type IconName } from '../../icons';

export type SuggestionChipVariant = 'suggestion' | 'generate' | 'starter';

const VARIANT_CLASS: Record<SuggestionChipVariant, string> = {
	// V1.1 option F (A7, N1, N2): white at 80% inside an ember ring at 45%, deepening to 70% on hover. No icon.
	suggestion: cn(
		'px-3 font-medium bg-[var(--ds-suggestion-chip-bg)] [box-shadow:var(--ds-elevation-chip)]',
		'transition-[box-shadow,background-color,color] [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)] motion-reduce:transition-none',
		'enabled:hover:[box-shadow:var(--ds-elevation-chip-hover)]',
		// Pressed is ink (V1.1 chips: action.primary with text.inverse).
		'enabled:active:bg-[var(--ds-action-primary)] enabled:active:text-[var(--ds-text-inverse)] enabled:active:[box-shadow:none]',
		'focus-visible:[box-shadow:var(--ds-elevation-chip),var(--ds-focus-ring)]'
	),
	// The Starters pill (A6): white at 75%, a 1px inset edge, the pen icon, regular weight.
	generate: cn(
		'gap-[7px] pl-2.5 pr-3 bg-[var(--ds-generate-chip-bg)] [box-shadow:inset_0_0_0_1px_var(--ds-action-secondary-border)]',
		'transition-colors [transition-duration:var(--ds-dur-instant)] [transition-timing-function:var(--ds-ease-standard)] enabled:hover:bg-[var(--ds-surface-default)]',
		'focus-visible:[box-shadow:inset_0_0_0_1px_var(--ds-action-secondary-border),var(--ds-focus-ring)]'
	),
	// The hero Starters (U22): white at 32% on the wash, secondary ink, a muted icon. Pressed reads white and ink.
	starter: cn(
		'gap-[7px] pl-2.5 pr-3 text-[var(--ds-text-secondary)] bg-[var(--ds-starter-chip-bg)] [box-shadow:inset_0_0_0_1px_var(--ds-starter-chip-edge)] [&>svg]:text-[var(--ds-text-muted)]',
		'transition-colors [transition-duration:var(--ds-dur-instant)] [transition-timing-function:var(--ds-ease-standard)] enabled:hover:bg-[var(--ds-generate-chip-bg)]',
		'aria-pressed:bg-[var(--ds-surface-default)] aria-pressed:font-medium aria-pressed:text-[var(--ds-text-primary)] aria-pressed:[&>svg]:text-[var(--ds-text-primary)]',
		'focus-visible:[box-shadow:inset_0_0_0_1px_var(--ds-starter-chip-edge),var(--ds-focus-ring)]'
	)
};

export interface SuggestionChipProps {
	/** The suggestion, sent as the message when clicked. */
	text: string;
	onSelect: (text: string) => void;
	/** `generate`: the pill that skips the optional questions (A6). `starter`: a hero Starter (U22). */
	variant?: SuggestionChipVariant;
	/** The leading glyph. `generate` defaults to the pen; `suggestion` has none (V1.1 option F). */
	icon?: IconName;
	/** `starter`: chosen, while it steers the composer (its placeholder). */
	pressed?: boolean;
	disabled?: boolean;
	className?: string;
}

/**
 * A chip above the composer (Eeva V1 board 3). `suggestion` (A7, V1.1 option F):
 * a next message, 30px, 13/500, white at 80% inside a 1px ember ring that
 * deepens on hover; pressed is ink. `generate` (A6): Skip questions, generate
 * my brief. `starter`: the hero Starters (U22).
 *
 * @status   draft
 * @variants variant: suggestion | generate | starter; pressed; disabled
 * @use      `suggestion`: two to four in the Composer's `suggestions` slot, a fixed row from turn 1, refreshed each turn, Draft copy first on copy-bearing types (N1, N2); clicking sends the text. `generate`: the Composer's `generateChip` slot, right-aligned, while optional questions remain.
 * @avoid    Not while the agent is working. A docked question keeps the row (N1). Never `generate` on a required question.
 */
export function SuggestionChip({
	text,
	onSelect,
	variant = 'suggestion',
	icon = variant === 'generate' ? 'pen' : undefined,
	pressed,
	disabled = false,
	className
}: SuggestionChipProps) {
	return (
		<button
			aria-pressed={pressed}
			className={cn(
				'inline-flex h-[30px] items-center whitespace-nowrap rounded-[var(--ds-radius-pill)] text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none',
				VARIANT_CLASS[variant],
				'disabled:cursor-not-allowed disabled:opacity-40',
				className
			)}
			disabled={disabled}
			type='button'
			onClick={() => onSelect(text)}
		>
			{icon ? <Icon name={icon} size={14} /> : null}
			{text}
		</button>
	);
}
