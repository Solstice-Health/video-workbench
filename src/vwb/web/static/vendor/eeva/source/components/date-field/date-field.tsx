'use client';

import { forwardRef, useImperativeHandle, useRef, useState } from 'react';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { FIELD_ICON_CLASS, FIELD_INPUT_CLASS, FieldShell } from '../field-shell/field-shell';

const DISPLAY = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });

/** `2026-10-16` -> `Oct 16, 2026`. Anything unparseable comes back as is. */
export function formatDateValue(iso: string) {
	if (!iso) {
		return '';
	}
	const date = new Date(`${iso}T00:00:00Z`);
	return Number.isNaN(date.getTime()) ? iso : DISPLAY.format(date);
}

export interface DateFieldProps extends Pick<
	React.InputHTMLAttributes<HTMLInputElement>,
	'id' | 'name' | 'required' | 'aria-label' | 'aria-describedby'
> {
	/** ISO date, `YYYY-MM-DD`. Empty string for no date. */
	value?: string;
	defaultValue?: string;
	onValueChange?: (value: string) => void;
	error?: boolean;
	placeholder?: string;
	className?: string;
}

/**
 * A date (Eeva V1.1, boards 6 to 9: Tracking details' optional dates). The
 * field shell with a calendar icon and mm/dd/yyyy; reads as `Oct 16, 2026` at
 * rest and becomes the browser's native date entry and picker while editing.
 *
 * @status   draft
 * @variants error
 * @use      PRC submission, AfD target and Live date, under Dates · optional in Tracking details.
 * @avoid    Not for a date range or a time.
 */
export const DateField = forwardRef<HTMLInputElement, DateFieldProps>(function DateField(
	{ value, defaultValue = '', onValueChange, error = false, placeholder = 'mm/dd/yyyy', className, ...props },
	ref
) {
	const inputRef = useRef<HTMLInputElement>(null);
	useImperativeHandle(ref, () => inputRef.current as HTMLInputElement);
	const [editing, setEditing] = useState(false);
	const [ownValue, setOwnValue] = useState(defaultValue);
	const current = value ?? ownValue;

	return (
		<FieldShell className={className} error={error}>
			<Icon className={FIELD_ICON_CLASS} name='calendar' size={14} />
			<input
				ref={inputRef}
				aria-invalid={error || undefined}
				className={cn(FIELD_INPUT_CLASS, '[&::-webkit-calendar-picker-indicator]:hidden')}
				placeholder={placeholder}
				readOnly={!editing}
				type={editing ? 'date' : 'text'}
				value={editing ? current : formatDateValue(current)}
				onBlur={() => setEditing(false)}
				onChange={event => {
					if (!editing) {
						return;
					}
					setOwnValue(event.target.value);
					onValueChange?.(event.target.value);
				}}
				onFocus={() => setEditing(true)}
				// Opened on press, a frame after the field turns into a date input: the first click's own click event
				// is lost to that switch. showPicker throws outside a gesture or where unsupported; typing still works.
				onPointerDown={() => {
					setEditing(true);
					requestAnimationFrame(() => {
						try {
							inputRef.current?.showPicker();
						} catch {
							/* keyboard entry remains */
						}
					});
				}}
				{...props}
			/>
		</FieldShell>
	);
});
