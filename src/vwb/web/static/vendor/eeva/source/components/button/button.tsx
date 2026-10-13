'use client';

import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

export type ButtonVariant = 'primary' | 'secondary' | 'surface' | 'destructive' | 'text';
export type ButtonSize = 'sm' | 'md';

// Pressed (`active`) is not drawn on the boards: one step past hover, no movement (added Oct 6).
const VARIANT_CLASS: Record<ButtonVariant, string> = {
	primary:
		'bg-[var(--ds-action-primary)] text-[var(--ds-text-inverse)] enabled:hover:bg-[var(--ds-action-primary-hover)] enabled:active:bg-[var(--ds-action-primary-pressed)]',
	// The edge is a box-shadow, so the focus ring has to carry it too or it vanishes on focus.
	secondary:
		'bg-transparent text-[var(--ds-text-primary)] [box-shadow:inset_0_0_0_1px_var(--ds-action-secondary-border)] enabled:hover:bg-[var(--ds-action-hover-tint)] enabled:active:bg-[var(--ds-action-pressed-tint)] focus-visible:[box-shadow:inset_0_0_0_1px_var(--ds-action-secondary-border),var(--ds-focus-ring)]',
	// White with a 1px border.strong edge: the composer's Stop (board 3, Oct 5).
	surface:
		'bg-[var(--ds-surface-default)] text-[var(--ds-text-primary)] [box-shadow:inset_0_0_0_1px_var(--ds-border-strong)] enabled:hover:bg-[var(--ds-surface-band)] enabled:active:bg-[var(--ds-action-surface-pressed)] focus-visible:[box-shadow:inset_0_0_0_1px_var(--ds-border-strong),var(--ds-focus-ring)]',
	destructive:
		'bg-[var(--ds-action-destructive)] text-[var(--ds-text-inverse)] enabled:hover:bg-[var(--ds-action-destructive-hover)] enabled:active:bg-[var(--ds-action-destructive-pressed)]',
	text: 'bg-transparent text-[var(--ds-text-secondary)] enabled:hover:bg-[var(--ds-action-hover-tint)] enabled:active:bg-[var(--ds-action-pressed-tint)]'
};

const SIZE_CLASS: Record<ButtonSize, string> = {
	sm: 'h-[var(--ds-size-sm)] px-3 text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)]',
	md: 'h-[var(--ds-size-md)] px-3.5 text-[length:var(--ds-type-control-size)] leading-[var(--ds-type-control-leading)]'
};

export interface ButtonProps extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'type'> {
	variant?: ButtonVariant;
	size?: ButtonSize;
	/** A 32px circle at any size (board 3: Attach, Send, Stop, Close). Requires `aria-label`; pair it with a `Tooltip`. */
	iconOnly?: boolean;
	/** Keeps the label, adds the animated dots and blocks clicks until it resolves. */
	loading?: boolean;
	/** Always a real button; `submit` only inside a form. */
	type?: 'button' | 'submit';
}

/**
 * The one button for every surface (Eeva V1 board 3, Oct 5).
 *
 * @status   draft
 * @variants variant: primary | secondary | surface | destructive | text; size: sm | md; iconOnly; disabled; loading; pressed
 * @use      `primary`: one per view, the main action (Submit request, Send). `secondary`: everything else, and the panel header's waiting state (Preparing or Updating request) with `loading`, 60% and animated dots (board 3). `surface`: the composer's Stop. `destructive`: an action that loses work (Change and start over). `text`: low emphasis (Skip, Finish with what we have, Attach).
 * @avoid    Navigation inside a sentence is `TextLink`. Never two `primary` buttons in one view.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
	{
		variant = 'secondary',
		size = 'md',
		iconOnly = false,
		loading = false,
		disabled = false,
		className,
		type = 'button',
		children,
		...props
	},
	ref
) {
	return (
		<button
			ref={ref}
			aria-busy={loading || undefined}
			className={cn(
				'inline-flex shrink-0 items-center justify-center gap-1.5 whitespace-nowrap rounded-[var(--ds-radius-pill)] font-medium outline-none transition-colors [transition-duration:var(--ds-dur-instant)] [transition-timing-function:var(--ds-ease-standard)] focus-visible:[box-shadow:var(--ds-focus-ring)]',
				iconOnly ? 'size-8' : SIZE_CLASS[size],
				VARIANT_CLASS[variant],
				iconOnly &&
					variant === 'text' &&
					'enabled:hover:bg-[var(--ds-action-icon-hover-tint)] enabled:hover:text-[var(--ds-text-primary)] enabled:active:bg-[var(--ds-action-pressed-tint)]',
				loading
					? 'cursor-progress opacity-60'
					: cn('disabled:cursor-not-allowed', iconOnly ? 'disabled:opacity-35' : 'disabled:opacity-40'),
				className
			)}
			disabled={disabled || loading}
			type={type === 'submit' ? 'submit' : 'button'}
			{...props}
		>
			{children}
			{loading && !iconOnly ? (
				<span aria-hidden='true' className='-ml-1.5 inline-flex'>
					<span className='animate-ds-dot'>.</span>
					<span className='animate-ds-dot [animation-delay:200ms]'>.</span>
					<span className='animate-ds-dot [animation-delay:400ms]'>.</span>
				</span>
			) : null}
		</button>
	);
});
