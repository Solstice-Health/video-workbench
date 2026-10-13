'use client';

import * as TabsPrimitive from '@radix-ui/react-tabs';

import { cn } from '@/lib/utils';

export const Tabs = TabsPrimitive.Root;
export const TabPanel = TabsPrimitive.Content;

export function TabList({ children, className, ...props }: React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>) {
	return (
		<TabsPrimitive.List className={cn('flex gap-6 [box-shadow:inset_0_-1px_0_var(--ds-tabs-rule)]', className)} {...props}>
			{children}
		</TabsPrimitive.List>
	);
}

export interface TabProps extends Omit<React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>, 'children'> {
	children: React.ReactNode;
	/** A count badge after the label. Hidden at 0. */
	count?: number;
}

/**
 * Underlined tabs with an optional count (Eeva V1 board 3). 40px, 24px
 * apart, 14px; the active tab is 600 with a 2px ink underline, the others
 * 500 in secondary text. The count badge is ink on the active tab.
 *
 * @status   draft
 * @variants count
 * @use      Switching views of one thing: Request Detail's Details and Files, the brand library's sources.
 * @avoid    Not for navigation between pages.
 */
export function Tab({ children, count, className, ...props }: TabProps) {
	return (
		<TabsPrimitive.Trigger
			className={cn(
				'group flex h-[var(--ds-size-md)] items-center gap-2 rounded-t-[var(--ds-radius-xs)] text-[length:var(--ds-type-control-size)] font-medium text-[var(--ds-text-secondary)] outline-none',
				'data-[state=active]:font-semibold data-[state=active]:text-[var(--ds-text-primary)] data-[state=active]:[box-shadow:inset_0_-2px_0_var(--ds-action-primary)]',
				'focus-visible:[box-shadow:var(--ds-focus-ring)]',
				className
			)}
			{...props}
		>
			{children}
			{count ? (
				<span className='inline-flex h-5 min-w-5 items-center justify-center rounded-[var(--ds-radius-pill)] bg-[var(--ds-count-badge-bg)] px-1.5 text-[11px] font-semibold text-[var(--ds-text-secondary)] group-data-[state=active]:bg-[var(--ds-action-primary)] group-data-[state=active]:text-[var(--ds-text-inverse)]'>
					{count}
				</span>
			) : null}
		</TabsPrimitive.Trigger>
	);
}
