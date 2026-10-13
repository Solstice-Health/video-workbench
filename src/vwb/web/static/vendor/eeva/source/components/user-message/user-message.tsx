import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { Button } from '../button/button';
import { Tooltip } from '../tooltip/tooltip';

export interface UserMessageProps {
	children: React.ReactNode;
	/** "4:24 PM", beneath the bubble. Replaced by the Queued line while queued. */
	time?: string;
	/** `AttachmentThumb`s (sm), in a row above the bubble. */
	attachments?: React.ReactNode;
	/** Sent while the agent works: the bubble at 60% and the Queued line under it. */
	queued?: boolean;
	/** Queued only: a trash on the Queued line takes the message back. */
	onRemoveQueued?: () => void;
	className?: string;
}

// The line under a queued message (board 3, from B1): bold Queued, then what happens next.
function QueuedLine({ onRemove }: { onRemove?: () => void }) {
	return (
		<span className='flex items-center gap-1 text-[length:var(--ds-type-caption-size)] leading-[var(--ds-type-caption-leading)] text-[var(--ds-text-muted)]'>
			<span>
				<strong className='font-bold text-[var(--ds-text-secondary)]'>Queued</strong> · sends when the agent finishes
			</span>
			{onRemove ? (
				<Tooltip content='Remove from the queue'>
					<Button
						aria-label='Remove queued message'
						className='-my-1 size-6 text-[var(--ds-text-tertiary)]'
						size='sm'
						variant='text'
						iconOnly
						onClick={onRemove}
					>
						<Icon name='trash' size={14} />
					</Button>
				</Tooltip>
			) : null}
		</span>
	);
}

/**
 * Your message (Eeva V1 board 4, from A1, C1, B1): a bubble on the right,
 * 15/24, at most 78% wide, radius 18, the time beneath and any files above,
 * 6px apart.
 *
 * @status   draft
 * @variants plain; with files; files only (no bubble); queued (removable)
 * @use      Everything the person sends, and the answer half of an `AnsweredPair`.
 * @avoid    No avatar, no name. The agent's words are `AgentMessage`.
 */
export function UserMessage({ children, time, attachments, queued = false, onRemoveQueued, className }: UserMessageProps) {
	// Files sent with no words: the thumbs stand alone, no empty bubble under them.
	const hasText = children != null && children !== false && !(typeof children === 'string' && !children.trim());
	return (
		// Board 8 rise in: base 220, standard, 6px up, on arrival.
		<div
			className={cn(
				'flex flex-col items-end gap-1.5',
				'animate-in fade-in-0 slide-in-from-bottom-[var(--ds-distance-rise)] [animation-duration:var(--ds-dur-base)] [animation-timing-function:var(--ds-ease-standard)] motion-reduce:animate-none',
				className
			)}
		>
			{attachments ? <div className='flex flex-wrap justify-end gap-1.5'>{attachments}</div> : null}
			{hasText ? (
				<div
					className={cn(
						'max-w-[78%] whitespace-pre-wrap break-words rounded-[var(--ds-radius-bubble)] bg-[var(--ds-surface-bubble)] px-4 py-2.5 text-[length:var(--ds-type-body-size)] leading-[var(--ds-type-body-leading)] text-[var(--ds-text-primary)]',
						queued && 'opacity-[var(--ds-opacity-queued)]'
					)}
				>
					{children}
				</div>
			) : null}
			{queued ? (
				<QueuedLine onRemove={onRemoveQueued} />
			) : time ? (
				<span className='text-[length:var(--ds-type-caption-size)] leading-[var(--ds-type-caption-leading)] text-[var(--ds-text-muted)]'>
					{time}
				</span>
			) : null}
		</div>
	);
}
