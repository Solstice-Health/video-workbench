'use client';

import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

import { Checkbox, type CheckboxProps } from '../checkbox/checkbox';

export interface ChecklistRowProps extends Omit<CheckboxProps, 'children' | 'className' | 'checked' | 'onCheckedChange'> {
	label: string;
	/** One line under the label. */
	description?: string;
	checked: boolean;
	onCheckedChange: (checked: boolean) => void;
	className?: string;
}

/**
 * One choice in a multi-select list (Eeva V1 board 3b, from F1): Checkbox,
 * label and subtext. A checked row takes the sunken fill; the whole row is
 * the click target.
 *
 * @status   draft
 * @variants checked; disabled
 * @use      A stack of rows 2px apart, like the multi-select `ComposerQuestion` (Which platforms?).
 * @avoid    One choice of several is the numbered option rows. A lone checkbox is `Checkbox`.
 */
export const ChecklistRow = forwardRef<HTMLButtonElement, ChecklistRowProps>(function ChecklistRow(
	{ label, description, checked, onCheckedChange, disabled, className, ...props },
	ref
) {
	return (
		// 48px as the boards render it: a 40px content box inside 4px padding.
		<label
			className={cn(
				'flex min-h-12 items-center gap-3 rounded-[var(--ds-radius-md)] px-2.5 py-1 transition-colors [transition-duration:var(--ds-dur-instant)] [transition-timing-function:var(--ds-ease-standard)]',
				checked && 'bg-[var(--ds-surface-sunken)]',
				disabled ? 'cursor-not-allowed' : 'cursor-pointer',
				className
			)}
		>
			<Checkbox
				ref={ref}
				checked={checked}
				disabled={disabled}
				onCheckedChange={value => onCheckedChange(value === true)}
				{...props}
			/>
			{/* The Checkbox fades itself when disabled; only the text needs it here. */}
			<span className={cn('flex min-w-0 flex-col', disabled && 'opacity-40')}>
				<span className='text-[length:var(--ds-type-control-size)] leading-[18px] text-[var(--ds-text-primary)]'>{label}</span>
				{description ? (
					<span className='text-[length:var(--ds-type-caption-size)] leading-[var(--ds-type-caption-leading)] text-[var(--ds-text-tertiary)]'>
						{description}
					</span>
				) : null}
			</span>
		</label>
	);
});
