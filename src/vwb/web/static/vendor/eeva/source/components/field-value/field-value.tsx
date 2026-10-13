'use client';

import * as PopoverPrimitive from '@radix-ui/react-popover';
import { useState } from 'react';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { Checkbox } from '../checkbox/checkbox';
import { FIELD_ICON_CLASS } from '../field-shell/field-shell';
import { SELECT_MENU_CLASS, SELECT_TRIGGER_CLASS, SelectField, type SelectOption } from '../select-field/select-field';
import { Skeleton } from '../skeleton/skeleton';
import { TextArea } from '../text-area/text-area';
import { fieldRingClass, TextField } from '../text-field/text-field';
import { UrlField } from '../url-field/url-field';

export type FieldValueKind = 'text' | 'textarea' | 'select' | 'checklist' | 'url';
export type FieldValueState = 'idle' | 'loading' | 'updated' | 'missing' | 'stopped';

/** `checklist` only: a typed choice after the list, e.g. Custom size…. */
export interface FieldValueCustom {
	label: string;
	/** The stored form of the typed text, or null when it is not a valid choice. */
	normalize: (text: string) => string | null;
}

// Hover keeps the mark; focus still shows the field's own editing ring.
const MARKED_CLASS = '[box-shadow:inset_0_0_0_1.5px_var(--ds-border-marked)] hover:[box-shadow:inset_0_0_0_1.5px_var(--ds-border-marked)]';

// A stopped, empty field is a bare box (B3): no chevron, no link or calendar glyph.
const STOPPED_CLASS: Record<FieldValueKind, string> = {
	text: '',
	textarea: '',
	select: '[&>span:last-child]:hidden',
	checklist: '[&>span:last-child]:hidden',
	url: '[&>svg]:hidden'
};

// The stored form of several choices: comma-joined values, "LinkedIn, Instagram".
const splitList = (value: string) =>
	value
		.split(',')
		.map(part => part.trim())
		.filter(Boolean);

export interface FieldValueProps {
	kind?: FieldValueKind;
	state?: FieldValueState;
	/** `checklist` values are comma-joined. */
	value: string;
	onValueChange?: (value: string) => void;
	/** `select` and `checklist`. */
	options?: SelectOption[];
	custom?: FieldValueCustom;
	placeholder?: string;
	id?: string;
	'aria-label'?: string;
	'aria-describedby'?: string;
	className?: string;
}

interface ChecklistFieldProps extends Omit<FieldValueProps, 'kind' | 'state' | 'onValueChange'> {
	options: SelectOption[];
	error: boolean;
	onValueChange: (value: string) => void;
}

function ChecklistField({ value, options, custom, onValueChange, placeholder, error, className, ...trigger }: ChecklistFieldProps) {
	const [typed, setTyped] = useState('');
	const [invalid, setInvalid] = useState(false);
	const chosen = splitList(value);
	// A typed choice (a custom size) is a row of its own, so toggling another keeps it.
	const rows = [
		...options,
		...chosen.filter(choice => !options.some(option => option.value === choice)).map(choice => ({ value: choice, label: choice }))
	];
	const shown = chosen.map(choice => rows.find(option => option.value === choice)?.label ?? choice).join(', ');
	// List order, so the closed field always reads the same way round.
	const toggle = (toggled: string, checked: boolean) =>
		onValueChange(
			rows
				.filter(option => (option.value === toggled ? checked : chosen.includes(option.value)))
				.map(option => option.value)
				.join(', ')
		);
	const addCustom = () => {
		const choice = custom?.normalize(typed);
		if (!choice) {
			setInvalid(true);
			return;
		}
		setTyped('');
		setInvalid(false);
		onValueChange([...new Set([...chosen, choice])].join(', '));
	};
	return (
		<PopoverPrimitive.Root>
			<PopoverPrimitive.Trigger
				{...trigger}
				aria-invalid={error || undefined}
				className={cn(
					SELECT_TRIGGER_CLASS,
					fieldRingClass(error),
					!error && 'data-[state=open]:[box-shadow:var(--ds-field-editing)]',
					className
				)}
				data-placeholder={shown ? undefined : ''}
			>
				<span className='min-w-0 flex-1 truncate'>{shown || placeholder}</span>
				<span>
					<Icon className={FIELD_ICON_CLASS} name='chevron-down' size={14} />
				</span>
			</PopoverPrimitive.Trigger>
			<PopoverPrimitive.Portal>
				{/* The field's own width, kept inside the window: a SelectField menu's compact rows with a checkbox (not drawn on board 3b). */}
				<PopoverPrimitive.Content
					align='start'
					className={cn(SELECT_MENU_CLASS, 'flex w-[var(--radix-popover-trigger-width)] flex-col')}
					collisionPadding={8}
					sideOffset={4}
				>
					{rows.map(option => (
						<label
							key={option.value}
							className='flex h-[var(--ds-size-sm)] cursor-pointer select-none items-center gap-2.5 rounded-[var(--ds-radius-md)] px-2.5 text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] hover:bg-[var(--ds-action-hover-tint)]'
						>
							<Checkbox
								checked={chosen.includes(option.value)}
								onCheckedChange={next => toggle(option.value, next === true)}
							/>
							<span className='min-w-0 truncate'>{option.label}</span>
						</label>
					))}
					{custom ? (
						<div className='px-1 pb-1 pt-1.5'>
							<TextField
								aria-label={custom.label}
								// An entry normalize rejects gets the error ring only (Eeva V1: no inline errors).
								error={invalid}
								placeholder={custom.label}
								value={typed}
								onChange={event => {
									setTyped(event.target.value);
									setInvalid(false);
								}}
								onKeyDown={event => {
									if (event.key === 'Enter') {
										event.preventDefault();
										addCustom();
									}
								}}
							/>
						</div>
					) : null}
				</PopoverPrimitive.Content>
			</PopoverPrimitive.Portal>
		</PopoverPrimitive.Root>
	);
}

/**
 * The value slot of a request panel row in one of its states (Eeva V1 board
 * 3b): the agent loading it, the agent having updated it, required and
 * missing, or left empty by a Stop. Renders the right field for `kind`.
 *
 * @status   draft
 * @variants kind: text | textarea | select | checklist | url; state: idle | loading | updated | missing | stopped
 * @use      Every row of RequestDocument. `loading` while the agent writes the value (A1, C1); `updated` after it rewrote one (C2), cleared by the first edit; `missing` once a required question is skipped (E1); `stopped` for a value the agent never reached (B3). `checklist` for several choices (Banner sizes, Platform), with `custom` for a typed one.
 * @avoid    Not for Notes (`RichTextArea`) or a file's note. No hint text under the field: the panel's MessageBar says what is wrong.
 */
export function FieldValue({
	kind = 'text',
	state = 'idle',
	value,
	onValueChange,
	options = [],
	custom,
	placeholder,
	id,
	className,
	...aria
}: FieldValueProps) {
	// The updated ring clears on the first edit, and comes back with the next update.
	const [edited, setEdited] = useState(false);
	const [seenState, setSeenState] = useState(state);
	if (state !== seenState) {
		setSeenState(state);
		setEdited(false);
	}

	if (state === 'loading') {
		return <Skeleton className={className} />;
	}

	const error = state === 'missing';
	const stopped = state === 'stopped' && !value;
	const fieldClass = cn(state === 'updated' && !edited && MARKED_CLASS, stopped && STOPPED_CLASS[kind], className);
	// Missing is the ring alone (E1): no placeholder, no hint.
	const shownPlaceholder = stopped ? 'Not set yet' : error ? '' : placeholder;
	const change = (next: string) => {
		setEdited(true);
		onValueChange?.(next);
	};
	const shared = { ...aria, className: fieldClass, error, id, placeholder: shownPlaceholder, value };

	switch (kind) {
		case 'select':
			return <SelectField {...shared} options={options} onValueChange={change} />;
		case 'checklist':
			return <ChecklistField {...shared} custom={custom} options={options} onValueChange={change} />;
		case 'textarea':
			return <TextArea {...shared} readOnly={!onValueChange} rows={2} onChange={event => change(event.target.value)} />;
		default: {
			const Field = kind === 'url' ? UrlField : TextField;
			return <Field {...shared} readOnly={!onValueChange} onChange={event => change(event.target.value)} />;
		}
	}
}
