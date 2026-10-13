'use client';

import * as DropdownMenu from '@radix-ui/react-dropdown-menu';

import { Icon, type IconName } from '../../icons';
import { Tooltip } from '../tooltip/tooltip';

export interface AttachMenuOption {
	label: string;
	icon: IconName;
	onSelect: () => void;
}

export interface AttachMenuProps {
	/** Upload files, then Select from brand library (M1). */
	options: AttachMenuOption[];
	/** The trigger: the composer's + or the panel's Add files. */
	children: React.ReactElement;
	/** Above the composer's + (top, start); below the panel's Add files (bottom, end). */
	side?: 'top' | 'bottom';
	align?: 'start' | 'end';
	/** Nudges the menu along the trigger, e.g. -6 to line up with the composer's text (M1). */
	alignOffset?: number;
	/** A hover label on the trigger, like Attach files on the composer's +. */
	tooltip?: string;
}

/**
 * Where a file comes from (Eeva V1 board 3, from M1 and M2): the same
 * dropdown behind the composer's + and the panel's Add files. 232px, radius 10,
 * 36px items with a 15px icon; the hovered item takes an ink 5% fill.
 *
 * @status   draft
 * @use      Wrap the + or Add files button; the items open the file picker or the brand library.
 * @avoid    Not a general menu: two or three attachment sources only.
 */
export function AttachMenu({ options, children, side = 'top', align = 'start', alignOffset = 0, tooltip }: AttachMenuProps) {
	const trigger = <DropdownMenu.Trigger asChild>{children}</DropdownMenu.Trigger>;
	return (
		<DropdownMenu.Root modal={false}>
			{tooltip ? <Tooltip content={tooltip}>{trigger}</Tooltip> : trigger}
			<DropdownMenu.Portal>
				<DropdownMenu.Content
					align={align}
					alignOffset={alignOffset}
					className='z-[var(--ds-z-dropdown)] w-[232px] animate-ds-popover-in rounded-[var(--ds-menu-radius)] bg-[var(--ds-surface-default)] p-1.5 font-[family-name:var(--ds-font-ui)] [box-shadow:var(--ds-elevation-menu)]'
					side={side}
					sideOffset={side === 'top' ? 8 : 6}
				>
					{options.map(option => (
						<DropdownMenu.Item
							key={option.label}
							className='flex h-9 cursor-default select-none items-center gap-2.5 whitespace-nowrap rounded-[var(--ds-menu-item-radius)] px-2.5 text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-primary)] outline-none data-[highlighted]:bg-[var(--ds-action-hover-tint)] [&>svg]:text-[var(--ds-text-secondary)]'
							onSelect={option.onSelect}
						>
							<Icon name={option.icon} size={15} />
							{option.label}
						</DropdownMenu.Item>
					))}
				</DropdownMenu.Content>
			</DropdownMenu.Portal>
		</DropdownMenu.Root>
	);
}
