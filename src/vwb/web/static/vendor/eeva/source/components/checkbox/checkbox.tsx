'use client';

import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import { Check } from 'lucide-react';
import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

export type CheckboxProps = Omit<React.ComponentPropsWithoutRef<typeof CheckboxPrimitive.Root>, 'asChild'>;

/**
 * A multiple-choice box (Eeva V1 board 3b). 18px, radius 4; a 1.5px ring at
 * rest that darkens on hover; checked is ink with a white check.
 *
 * @status   draft
 * @variants checked; disabled
 * @use      Choosing several of a set, like banner sizes or library items. Name it with a `<label>` or `aria-label`.
 * @avoid    A single on/off setting is not a checkbox here. One choice of several is `SelectField`.
 */
export const Checkbox = forwardRef<React.ElementRef<typeof CheckboxPrimitive.Root>, CheckboxProps>(function Checkbox(
	{ className, ...props },
	ref
) {
	return (
		<CheckboxPrimitive.Root
			ref={ref}
			className={cn(
				'peer inline-flex size-[18px] shrink-0 items-center justify-center rounded-[var(--ds-radius-xs)] bg-[var(--ds-surface-default)] text-[var(--ds-text-inverse)] outline-none',
				'[box-shadow:inset_0_0_0_1.5px_var(--ds-checkbox-edge)] enabled:hover:[box-shadow:inset_0_0_0_1.5px_var(--ds-border-focus)]',
				'data-[state=checked]:bg-[var(--ds-action-primary)] data-[state=checked]:[box-shadow:none]',
				'focus-visible:[box-shadow:var(--ds-focus-ring)] data-[state=checked]:focus-visible:[box-shadow:var(--ds-focus-ring)]',
				'disabled:cursor-not-allowed disabled:opacity-35',
				className
			)}
			{...props}
		>
			<CheckboxPrimitive.Indicator>
				<Check aria-hidden='true' size={12} strokeWidth={3} />
			</CheckboxPrimitive.Indicator>
		</CheckboxPrimitive.Root>
	);
});
