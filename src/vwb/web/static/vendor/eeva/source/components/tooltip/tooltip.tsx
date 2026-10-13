'use client';

import * as TooltipPrimitive from '@radix-ui/react-tooltip';

import { cn } from '@/lib/utils';

export interface TooltipProps {
	/** The label, or a rich tooltip's body (up to three lines). */
	content: string;
	/** Makes it a rich tooltip: a title above the body, up to 320px wide. */
	title?: string;
	/** A real keyboard shortcut, shown as a chip after the label. */
	shortcut?: string;
	side?: 'top' | 'bottom' | 'left' | 'right';
	children: React.ReactNode;
	/** Controlled visibility, for previews and tests. Leave unset for hover and focus. */
	open?: boolean;
}

/**
 * A hover and focus tooltip (Eeva V1 board 3). Ink, 12/16 500, radius 6,
 * max 240px, 6px arrow, 8px from its anchor. Shows after 400ms hover or at
 * once on keyboard focus; hides on leave, blur or Escape; flips to stay in
 * view. The board's fade is 120ms; this uses motion.fast (150ms), the
 * nearest token.
 *
 * @status   draft
 * @variants label; shortcut; rich (title)
 * @use      Label every icon-only button. A rich tooltip explains why, like what a file was attached for.
 * @avoid    Buttons with visible text get none. Never interactive: no links or buttons inside; that is a popover.
 */
export function Tooltip({ content, title, shortcut, side = 'top', children, open }: TooltipProps) {
	const rich = Boolean(title);
	return (
		<TooltipPrimitive.Provider delayDuration={400}>
			<TooltipPrimitive.Root open={open}>
				<TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
				<TooltipPrimitive.Portal>
					<TooltipPrimitive.Content
						className={cn(
							'z-[var(--ds-z-tooltip)] bg-[var(--ds-action-primary)] font-[family-name:var(--ds-font-ui)] text-[var(--ds-text-inverse)]',
							'animate-in fade-in-0 [animation-duration:var(--ds-dur-fast)] data-[state=closed]:animate-out data-[state=closed]:fade-out-0',
							rich
								? 'flex max-w-[320px] flex-col gap-0.5 rounded-[var(--ds-radius-md)] px-3 py-2.5 text-[length:var(--ds-type-ui-size)] [box-shadow:var(--ds-tooltip-rich-shadow)]'
								: 'inline-flex max-w-[240px] items-center rounded-[var(--ds-radius-sm)] px-2 py-1.5 text-[length:var(--ds-type-caption-size)] font-medium leading-[var(--ds-type-caption-leading)] [box-shadow:var(--ds-tooltip-shadow)]'
						)}
						side={side}
						sideOffset={8}
					>
						{rich ? (
							<>
								<span className='font-semibold leading-[18px]'>{title}</span>
								<span className='leading-[19px] text-[var(--ds-tooltip-rich-body)]'>{content}</span>
							</>
						) : (
							<>
								{content}
								{shortcut ? (
									<kbd className='ml-2 rounded-[var(--ds-radius-xs)] bg-[var(--ds-tooltip-shortcut-bg)] px-[5px] font-[family-name:inherit] text-[11px] text-[var(--ds-tooltip-shortcut-text)]'>
										{shortcut}
									</kbd>
								) : null}
							</>
						)}
						<TooltipPrimitive.Arrow className='fill-[var(--ds-action-primary)]' height={6} width={12} />
					</TooltipPrimitive.Content>
				</TooltipPrimitive.Portal>
			</TooltipPrimitive.Root>
		</TooltipPrimitive.Provider>
	);
}
