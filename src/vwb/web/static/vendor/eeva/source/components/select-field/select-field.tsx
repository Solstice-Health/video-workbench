'use client';

import * as SelectPrimitive from '@radix-ui/react-select';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { FIELD_ICON_CLASS } from '../field-shell/field-shell';
import { fieldRingClass } from '../text-field/text-field';

export interface SelectOption {
	value: string;
	label: string;
}

// Shared with FieldValue's checklist kind, so both closed fields and menus match.
export const SELECT_TRIGGER_CLASS =
	'flex h-[var(--ds-size-sm)] w-full items-center gap-2 rounded-[var(--ds-radius-md)] bg-[var(--ds-surface-default)] px-2.5 text-left text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none transition-shadow [transition-duration:var(--ds-dur-fast)] data-[placeholder]:text-[var(--ds-text-tertiary)]';

export const SELECT_MENU_CLASS =
	'z-[var(--ds-z-dropdown)] min-w-[var(--radix-select-trigger-width)] animate-ds-popover-in overflow-hidden rounded-[var(--ds-radius-lg)] bg-[var(--ds-surface-default)] p-1 font-[family-name:var(--ds-font-ui)] [box-shadow:var(--ds-elevation-panel)]';

export interface SelectFieldProps {
	options: SelectOption[];
	value?: string;
	defaultValue?: string;
	onValueChange?: (value: string) => void;
	placeholder?: string;
	/** Drawn inside the trigger before the value, for a compact count select: "Subject lines 3 ⌄" (E5, Copy's How many row). Pass `className='w-auto'`. Also the default aria-label. */
	label?: string;
	error?: boolean;
	id?: string;
	name?: string;
	'aria-label'?: string;
	'aria-describedby'?: string;
	className?: string;
}

/**
 * One choice of a few (Eeva V1 board 3b): the field shell with a trailing
 * chevron. Board 3b draws the closed field only; the open menu here is the
 * smallest one built from existing tokens until it is drawn.
 *
 * @status   draft
 * @variants error; label (compact count select)
 * @use      Priority, channel, format: a short closed list. With `label`, an auto-width count like Subject lines 3 in Copy's How many row (E5).
 * @avoid    Several choices at once are Checkboxes. Never for navigation.
 */
export function SelectField({ options, placeholder = 'Choose one', label, error = false, className, ...props }: SelectFieldProps) {
	const { 'aria-label': ariaLabel = label, 'aria-describedby': ariaDescribedBy, id, ...rootProps } = props;
	return (
		<SelectPrimitive.Root {...rootProps}>
			<SelectPrimitive.Trigger
				aria-describedby={ariaDescribedBy}
				aria-invalid={error || undefined}
				aria-label={ariaLabel}
				className={cn(
					SELECT_TRIGGER_CLASS,
					fieldRingClass(error),
					!error && 'data-[state=open]:[box-shadow:var(--ds-field-editing)]',
					label && 'gap-1.5 pr-2',
					className
				)}
				id={id}
			>
				{label ? <span className='shrink-0 whitespace-nowrap text-[var(--ds-text-secondary)]'>{label}</span> : null}
				<span className={cn('min-w-0 truncate', !label && 'flex-1')}>
					<SelectPrimitive.Value placeholder={placeholder} />
				</span>
				<SelectPrimitive.Icon asChild>
					<span>
						<Icon className={FIELD_ICON_CLASS} name='chevron-down' size={14} />
					</span>
				</SelectPrimitive.Icon>
			</SelectPrimitive.Trigger>
			<SelectPrimitive.Portal>
				<SelectPrimitive.Content className={SELECT_MENU_CLASS} position='popper' sideOffset={4}>
					<SelectPrimitive.Viewport>
						{options.map(option => (
							<SelectPrimitive.Item
								key={option.value}
								className='flex h-[var(--ds-size-sm)] cursor-default select-none items-center gap-2 rounded-[var(--ds-radius-md)] px-2.5 text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none data-[highlighted]:bg-[var(--ds-action-hover-tint)]'
								value={option.value}
							>
								<SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
								<SelectPrimitive.ItemIndicator className='ml-auto'>
									<Icon name='check' size={14} />
								</SelectPrimitive.ItemIndicator>
							</SelectPrimitive.Item>
						))}
					</SelectPrimitive.Viewport>
				</SelectPrimitive.Content>
			</SelectPrimitive.Portal>
		</SelectPrimitive.Root>
	);
}
