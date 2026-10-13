'use client';

import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

// Shared by every board 3b field: 32px, radius 8, white, 10px inset, 13/20 text.
export const FIELD_BOX_CLASS =
	'rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 text-[length:var(--ds-type-ui-size)] leading-[var(--ds-type-ui-leading)] text-[var(--ds-text-primary)] outline-none placeholder:text-[var(--ds-text-tertiary)] transition-shadow [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)]';

export function fieldRingClass(error: boolean) {
	return error
		? '[box-shadow:var(--ds-field-error)]'
		: '[box-shadow:var(--ds-field-rest)] hover:[box-shadow:var(--ds-field-hover)] focus:[box-shadow:var(--ds-field-editing)] focus-within:[box-shadow:var(--ds-field-editing)]';
}

export interface TextFieldProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled'> {
	error?: boolean;
}

/**
 * Single-line text entry (Eeva V1 board 3b). 32px, radius 8, white with a
 * 12% ring at rest, 20% on hover, an ink ring and halo while editing.
 *
 * @status   draft
 * @variants error
 * @use      A short string in a row or small form: Requester, Topic, Goal.
 * @avoid    No disabled state: a read-only value renders as plain text. A link is `UrlField`.
 */
export const TextField = forwardRef<HTMLInputElement, TextFieldProps>(function TextField({ className, error = false, ...props }, ref) {
	return (
		<input
			ref={ref}
			aria-invalid={error || undefined}
			className={cn('h-[var(--ds-size-sm)] w-full', FIELD_BOX_CLASS, fieldRingClass(error), className)}
			{...props}
		/>
	);
});
