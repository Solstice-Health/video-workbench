'use client';

import { forwardRef } from 'react';

import { cn } from '@/lib/utils';

import { FIELD_BOX_CLASS, fieldRingClass } from '../text-field/text-field';

export interface TextAreaProps extends Omit<React.TextareaHTMLAttributes<HTMLTextAreaElement>, 'disabled' | 'rows'> {
	/** 2 for Goal, 4 for Notes (board 3b). */
	rows?: 2 | 4;
	error?: boolean;
}

/**
 * Multi-line text entry (Eeva V1 board 3b). The same rings as `TextField`,
 * 8px top and bottom inset, sized by its visible lines.
 *
 * @status   draft
 * @variants rows: 2 | 4; error
 * @use      Goal (2 lines), Notes (4 lines), a note on a file.
 * @avoid    Not for a single short value; that is `TextField`.
 */
export const TextArea = forwardRef<HTMLTextAreaElement, TextAreaProps>(function TextArea(
	{ className, rows = 4, error = false, ...props },
	ref
) {
	return (
		<textarea
			ref={ref}
			aria-invalid={error || undefined}
			className={cn('block w-full resize-y py-2', FIELD_BOX_CLASS, fieldRingClass(error), className)}
			rows={rows}
			{...props}
		/>
	);
});
