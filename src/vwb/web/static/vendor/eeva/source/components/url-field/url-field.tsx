'use client';

import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { FIELD_ICON_CLASS, FIELD_INPUT_CLASS, FieldShell } from '../field-shell/field-shell';

export interface UrlFieldProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled' | 'type'> {
	error?: boolean;
}

/**
 * A web address (Eeva V1 board 3b): the field shell with a leading link
 * icon. The value reads in the accent text colour in every state; on error
 * the icon turns red too, with no text (Eeva V1: no inline errors).
 *
 * @status   draft
 * @variants error
 * @use      CTA link, any URL the request points at.
 * @avoid    Not for free text. Validation belongs to the caller; this only shows the result.
 */
export const UrlField = forwardRef<HTMLInputElement, UrlFieldProps>(function UrlField(
	{ className, error = false, placeholder = 'Add a link', ...props },
	ref
) {
	return (
		<FieldShell className={className} error={error}>
			<Icon className={error ? 'shrink-0 text-[var(--ds-status-danger)]' : FIELD_ICON_CLASS} name='link' size={14} />
			<input
				ref={ref}
				aria-invalid={error || undefined}
				className={cn(FIELD_INPUT_CLASS, 'text-[var(--ds-text-accent)]')}
				inputMode='url'
				placeholder={placeholder}
				type='text'
				{...props}
			/>
		</FieldShell>
	);
});
