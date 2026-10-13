'use client';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { Tooltip } from '../tooltip/tooltip';

export type MessageBarTone = 'progress' | 'info' | 'success' | 'error';

const TONE_CLASS: Record<MessageBarTone, string> = {
	progress: 'bg-[var(--ds-surface-band)] text-[var(--ds-text-secondary)]',
	info: 'bg-[var(--ds-surface-band)] text-[var(--ds-text-secondary)]',
	success: 'bg-[var(--ds-status-success-bg)] text-[var(--ds-status-success)]',
	error: 'bg-[var(--ds-status-danger-bg)] text-[var(--ds-status-danger)]'
};

function ToneIcon({ tone }: { tone: MessageBarTone }) {
	if (tone === 'progress') {
		return (
			<span className='size-3.5 animate-spin rounded-full border-2 border-[var(--ds-spinner-track)] border-t-[var(--ds-text-primary)] [animation-duration:var(--ds-dur-spinner)] motion-reduce:animate-none' />
		);
	}
	if (tone === 'success') {
		return <Icon className='[stroke-width:2.25]' name='check' size={14} />;
	}
	// Error bars carry the triangle (E1); a field's own alert stays the circle.
	return <Icon name={tone === 'error' ? 'triangle-alert' : 'info'} size={14} />;
}

export interface MessageBarProps {
	tone: MessageBarTone;
	children: React.ReactNode;
	/** Shows the ×. `info` and `success` only: progress and error stay until their state ends. */
	onDismiss?: () => void;
	className?: string;
}

/**
 * One line of status at the top of the request panel body (Eeva V1 board 5,
 * from C1, C2, E1, B3, G3). Sand band, 13/18 text, a 14px icon aligned to the
 * first line; info and success bars carry an ×. Success is sage: the request
 * is ready to review (A5).
 *
 * @status   draft
 * @variants tone: progress | info | success | error; onDismiss (info, success)
 * @use      `progress` while the agent prepares or updates (Preparing your request…, Updating Audience and Topic…). `info` for what just happened (Updated, Stopped, Set up without reading a file). `success` once, when the request first becomes ready (Ready to review). `error` for a blocking gap (1 required field is still empty).
 * @avoid    One bar at a time. Never in the chat, never a toast. An error about one field also rings that field.
 */
export function MessageBar({ tone, children, onDismiss, className }: MessageBarProps) {
	const dismissible = (tone === 'info' || tone === 'success') && Boolean(onDismiss);
	return (
		<div
			className={cn(
				'flex min-h-9 items-start gap-2 rounded-[var(--ds-radius-md)] px-2.5 py-2 text-[length:var(--ds-type-ui-size)] leading-[18px]',
				TONE_CLASS[tone],
				className
			)}
			role={tone === 'error' ? 'alert' : 'status'}
		>
			<span aria-hidden='true' className='mt-0.5 flex shrink-0'>
				<ToneIcon tone={tone} />
			</span>
			<span className='min-w-0 flex-1'>{children}</span>
			{dismissible ? (
				<Tooltip content='Dismiss'>
					<button
						aria-label='Dismiss'
						className='-m-1 ml-auto flex shrink-0 rounded-[var(--ds-radius-xs)] p-1 opacity-80 outline-none transition-opacity [transition-duration:var(--ds-dur-fast)] hover:opacity-100 focus-visible:[box-shadow:var(--ds-focus-ring)]'
						type='button'
						onClick={onDismiss}
					>
						<Icon name='x' size={14} />
					</button>
				</Tooltip>
			) : null}
		</div>
	);
}
