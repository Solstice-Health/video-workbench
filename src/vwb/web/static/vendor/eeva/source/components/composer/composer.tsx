'use client';

import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { AttachMenu, type AttachMenuOption } from '../attach-menu/attach-menu';
import { Button } from '../button/button';
import { Tooltip } from '../tooltip/tooltip';

// One 24px line at rest, growing to eight, then the box scrolls.
const MAX_HEIGHT_PX = 192;

/** The mic. The caller records (`useDictation` in the intake); the box is read-only while it runs. */
interface ComposerDictation {
	state: 'idle' | 'recording' | 'transcribing';
	/** While recording, shown on the stop pill as 0:12. */
	elapsedMs: number;
	onStart: () => void;
	onStop: () => void;
}

export interface ComposerProps {
	value: string;
	onValueChange: (value: string) => void;
	/** Enter or Send, with the trimmed text. While the agent works the caller queues it. */
	onSend: (value: string) => void;
	/** False while nothing can go yet (files still reading or failed): Send disables and Enter does nothing. */
	canSend?: boolean;
	/** Shows the Attach button. */
	onAttach?: () => void;
	/** Attach opens the AttachMenu with these sources (Upload files, Select from brand library) instead of calling `onAttach`. */
	attachOptions?: AttachMenuOption[];
	/** Shows the mic, left of Send. */
	dictation?: ComposerDictation;
	/** The text box's paste, so the caller can turn a long one into an attachment. */
	onPaste?: React.ClipboardEventHandler<HTMLTextAreaElement>;
	/** With `working`, shows Stop 6px left of Send. */
	onStop?: () => void;
	/** The agent is working: Send stays and queues, Stop appears. */
	working?: boolean;
	placeholder?: string;
	/** The text box's accessible name. */
	label?: string;
	/** Staged `AttachmentThumb`s (with `onRemove`), wrapping in a row above the text (M5, M6). */
	attachments?: React.ReactNode;
	/** A file is being dragged over the page: the box shows Drop to attach (M7). The caller handles the drop. */
	dragging?: boolean;
	/** A docked `ComposerQuestion`, above a hairline; the text box below becomes its typed answer. */
	question?: React.ReactNode;
	/** Two to four `SuggestionChip`s, 8px above the composer. */
	suggestions?: React.ReactNode;
	/** A `SuggestionChip variant='generate'`, right-aligned 8px above the composer. */
	generateChip?: React.ReactNode;
	/** Pinned to the foot of the feed: adds the backing that blurs and fades the feed behind it. */
	docked?: boolean;
	className?: string;
}

/**
 * Where the person writes (Eeva V1 board 4, from A5 and B1). White, radius
 * 16, the composer lift and no grey border; it lifts further on focus. At
 * rest: placeholder, Attach and Send. While the agent works Send stays and
 * queues, and a round Stop appears beside it.
 *
 * @status   draft
 * @variants at rest; working; with files; with a question; with chips; docked; attach menu; dictation; dragging
 * @use      Once, at the foot of the chat. Questions, chips and files go in its slots.
 * @avoid    Not a form field: one-line answers in the panel are `TextField`. Never two composers in a view.
 */
export const Composer = forwardRef<HTMLTextAreaElement, ComposerProps>(function Composer(
	{
		value,
		onValueChange,
		onSend,
		canSend = true,
		onAttach,
		attachOptions,
		dictation,
		onPaste,
		onStop,
		working = false,
		placeholder = 'Reply, or ask me anything about this request…',
		label = 'Message',
		attachments,
		dragging = false,
		question,
		suggestions,
		generateChip,
		docked = false,
		className
	},
	ref
) {
	const textareaRef = useRef<HTMLTextAreaElement>(null);
	const dictating = Boolean(dictation && dictation.state !== 'idle');
	const elapsed = dictation ? Math.floor(dictation.elapsedMs / 1000) : 0;
	const recordedFor = `${Math.floor(elapsed / 60)}:${String(elapsed % 60).padStart(2, '0')}`;
	useImperativeHandle(ref, () => textareaRef.current as HTMLTextAreaElement);

	useEffect(() => {
		const node = textareaRef.current;
		if (!node) {
			return;
		}
		node.style.height = 'auto';
		node.style.height = `${Math.min(node.scrollHeight, MAX_HEIGHT_PX)}px`;
		// Follow the newest dictated words once the box is at its full height.
		if (dictating) {
			node.scrollTop = node.scrollHeight;
		}
		// The placeholder counts too: a long one (a mic error) wraps rather than clips.
	}, [value, dictating, placeholder]);

	const send = () => {
		const text = value.trim();
		// The live dictation owns the box until it's done; its partial words don't go.
		if (!canSend || dictating || (!text && !attachments)) {
			return;
		}
		onSend(text);
	};

	return (
		<div className={cn('w-full min-w-0', docked && 'relative pb-6 pt-11', className)}>
			{docked ? (
				<div
					aria-hidden='true'
					className='pointer-events-none absolute -inset-x-12 inset-y-0 bg-[image:var(--ds-composer-backing)] backdrop-blur-[var(--ds-composer-backing-blur)] [mask-image:var(--ds-composer-backing-mask)]'
				/>
			) : null}
			<div className='relative'>
				{generateChip ? <div className='mb-2 flex justify-end'>{generateChip}</div> : null}
				{suggestions ? (
					<div aria-label='Suggestions' className='mb-2 flex flex-wrap gap-2' role='group'>
						{suggestions}
					</div>
				) : null}
				<div className='relative'>
					<div className='overflow-hidden rounded-[var(--ds-radius-2xl)] bg-[var(--ds-surface-default)] transition-shadow [box-shadow:var(--ds-elevation-composer)] [transition-duration:var(--ds-dur-fast)] [transition-timing-function:var(--ds-ease-standard)] focus-within:[box-shadow:var(--ds-elevation-composer-focus)]'>
						{question ? (
							<>
								{question}
								<div className='mx-4 h-px bg-[var(--ds-border-divider)]' />
							</>
						) : null}
						<div className='flex flex-col gap-1 pb-2 pl-4 pr-2.5 pt-2.5'>
							{/* M5: the thumbs wrap 6px apart; no scroller, so their 1px edge is never clipped. */}
							{attachments ? <div className='flex flex-wrap items-center gap-1.5 pb-1.5 pt-0.5'>{attachments}</div> : null}
							<textarea
								ref={textareaRef}
								aria-busy={dictating || undefined}
								aria-label={label}
								className='block h-6 max-h-48 w-full resize-none bg-transparent text-[length:var(--ds-type-body-size)] leading-[var(--ds-type-body-leading)] text-[var(--ds-text-primary)] outline-none placeholder:text-[var(--ds-text-muted)]'
								placeholder={placeholder}
								// The live dictation owns the box until it's done; typing waits.
								readOnly={dictating}
								rows={1}
								value={value}
								onChange={event => onValueChange(event.target.value)}
								onKeyDown={event => {
									if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
										event.preventDefault();
										send();
									}
								}}
								onPaste={onPaste}
							/>
							<div className='flex items-center justify-between'>
								{attachOptions?.length ? (
									<AttachMenu alignOffset={-6} options={attachOptions} tooltip='Attach files'>
										<Button aria-label='Attach files' className='-ml-1.5' variant='text' iconOnly>
											<Icon name='plus' size={18} />
										</Button>
									</AttachMenu>
								) : onAttach ? (
									<Tooltip content='Attach files'>
										<Button aria-label='Attach files' className='-ml-1.5' variant='text' iconOnly onClick={onAttach}>
											<Icon name='plus' size={18} />
										</Button>
									</Tooltip>
								) : (
									<span />
								)}
								<div className='flex items-center gap-1.5'>
									{dictation?.state === 'recording' ? (
										<Tooltip content='Finish dictating'>
											<Button
												aria-label={`Stop dictation (${recordedFor})`}
												className='gap-2 px-2.5 tabular-nums'
												size='sm'
												variant='text'
												onClick={dictation.onStop}
											>
												<span
													aria-hidden='true'
													className='size-2 rounded-full bg-[var(--ds-status-danger)] motion-safe:animate-pulse'
												/>
												{recordedFor}
											</Button>
										</Tooltip>
									) : dictation ? (
										<Tooltip content={dictation.state === 'transcribing' ? 'Transcribing…' : 'Dictate'}>
											<Button
												aria-busy={dictation.state === 'transcribing' || undefined}
												aria-label={dictation.state === 'transcribing' ? 'Transcribing' : 'Dictate'}
												variant='text'
												iconOnly
												onClick={dictation.state === 'idle' ? dictation.onStart : undefined}
											>
												{dictation.state === 'transcribing' ? (
													<Icon className='motion-safe:animate-spin' name='loader' size={16} />
												) : (
													<Icon name='mic' size={16} />
												)}
											</Button>
										</Tooltip>
									) : null}
									{working && onStop ? (
										<Tooltip content='Stop'>
											<Button
												aria-label='Stop'
												className='animate-in fade-in-0 [animation-duration:var(--ds-dur-fast)]'
												variant='surface'
												iconOnly
												onClick={onStop}
											>
												<Icon name='square' size={14} />
											</Button>
										</Tooltip>
									) : null}
									<Tooltip content='Send'>
										<Button
											aria-label='Send'
											disabled={!canSend || dictating}
											variant='primary'
											iconOnly
											onClick={send}
										>
											<Icon name='arrow-up' />
										</Button>
									</Tooltip>
								</div>
							</div>
						</div>
					</div>
					{dragging ? (
						// M7: over the box, a soft ink ring inside its edge and a warm veil.
						<div
							aria-hidden='true'
							className='pointer-events-none absolute inset-0 z-[1] flex items-center justify-center gap-2.5 rounded-[var(--ds-radius-2xl)] bg-[var(--ds-drop-veil)] text-[length:var(--ds-type-control-size)] font-medium text-[var(--ds-text-primary)] animate-in fade-in-0 [animation-duration:var(--ds-dur-fast)] [box-shadow:var(--ds-drop-ring)] [&>svg]:text-[var(--ds-text-secondary)]'
						>
							<Icon name='upload' size={18} />
							Drop to attach
						</div>
					) : null}
				</div>
			</div>
		</div>
	);
});
