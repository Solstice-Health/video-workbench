'use client';

import { forwardRef } from 'react';

import { Icon } from '../../icons';
import { FIELD_ICON_CLASS, FIELD_INPUT_CLASS, FieldShell } from '../field-shell/field-shell';

export type SearchFieldProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, 'disabled' | 'type'>;

/**
 * Filters a list in place (Eeva V1 board 3b): the field shell at 40px with a
 * leading search icon.
 *
 * @status   draft
 * @use      The search above a library tab or a long list.
 * @avoid    It has no error state: an empty result is said in the list, not on the field.
 */
export const SearchField = forwardRef<HTMLInputElement, SearchFieldProps>(function SearchField({ className, ...props }, ref) {
	return (
		<FieldShell className={className} tall>
			<Icon className={FIELD_ICON_CLASS} name='search' size={14} />
			<input
				ref={ref}
				className={`${FIELD_INPUT_CLASS} [&::-webkit-search-cancel-button]:appearance-none`}
				type='search'
				{...props}
			/>
		</FieldShell>
	);
});
