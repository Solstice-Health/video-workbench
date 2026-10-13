'use client';

import { useState } from 'react';

import { cn } from '@/lib/utils';

import { Icon } from '../../icons';
import { SolsticeMark } from '../solstice-mark/solstice-mark';

export type ThinkingState = 'streaming' | 'done' | 'stopped' | 'failed';

/** One step: its label, a list under it if any, and how it ended. */
export interface ThinkingBlockStep {
	label: string;
	/** One line each under the label: "Reading 3 claims" and the three claims (#1053). */
	items?: string[];
	/** `failed`: the step the turn failed on; the list opens to show it. Unset reads done within `completed`. */
	status?: 'done' | 'failed';
}

export interface ThinkingBlockProps {
	state: ThinkingState;
	/** Streaming: the live thought, replaced as the next one arrives. */
	thought?: string;
	/** Done: what the turn did, "Read your message". */
	summary?: string;
	/** Every step of the turn, in order: a label, or a step with its list and status. */
	steps?: (string | ThinkingBlockStep)[];
	/** Steps finished so far. Defaults to all of them once done. */
	completed?: number;
	/** Whole seconds the turn has run. */
	seconds?: number;
	/** Failed: how many tries ran, "3 tries · failed" (G2). */
	attempts?: number;
	/** Controlled open state of the steps list. */
	expanded?: boolean;
	defaultExpanded?: boolean;
	onExpandedChange?: (expanded: boolean) => void;
	className?: string;
}

const ROW_TEXT = 'text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-muted)]';

function plural(count: number, noun: string) {
	return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

function rowLabel({
	state,
	summary,
	steps,
	completed,
	seconds,
	attempts
}: Pick<ThinkingBlockProps, 'state' | 'summary' | 'seconds' | 'attempts'> & { steps: ThinkingBlockStep[]; completed: number }) {
	const time = seconds === undefined ? null : `${seconds}s`;
	const progress = steps.length ? `${completed} of ${plural(steps.length, 'step')}` : null;
	if (state === 'failed') {
		return [attempts === undefined ? null : `${attempts} ${attempts === 1 ? 'try' : 'tries'}`, 'failed'].filter(Boolean).join(' · ');
	}
	if (state === 'stopped') {
		return ['Stopped', progress, time].filter(Boolean).join(' · ');
	}
	if (state === 'streaming') {
		return [progress, time].filter(Boolean).join(' · ');
	}
	return [summary, steps.length ? plural(steps.length, 'step') : null, time].filter(Boolean).join(' · ');
}

// A step's list (the claims being read): one bullet per item, each cut to one line.
function StepItems({ items }: { items: string[] }) {
	return (
		<ul className='m-0 list-disc pb-0.5 pl-4 marker:text-[var(--ds-text-muted)]'>
			{items.map((item, index) => (
				<li key={`${index}-${item}`} className='min-w-0 leading-5'>
					<span className='block truncate'>{item}</span>
				</li>
			))}
		</ul>
	);
}

function StepList({ steps, completed, open }: { steps: ThinkingBlockStep[]; completed: number; open: boolean }) {
	return (
		// Height eases open over motion.base (board 8, collapse and expand).
		<div
			aria-hidden={!open}
			className={cn(
				'grid transition-[grid-template-rows] [transition-duration:var(--ds-dur-base)] [transition-timing-function:var(--ds-ease-standard)] motion-reduce:transition-none',
				open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]'
			)}
		>
			<div className='min-h-0 overflow-hidden'>
				<ul className='m-0 ml-6 list-none p-0 pt-0.5'>
					{steps.map((step, index) => {
						const failed = step.status === 'failed';
						const done = step.status === 'done' || (step.status === undefined && index < completed);
						return (
							<li
								key={`${index}-${step.label}`}
								className={cn(
									'flex items-start gap-2 text-[length:var(--ds-type-ui-size)]',
									failed
										? 'text-[var(--ds-text-primary)]'
										: done
											? 'text-[var(--ds-text-tertiary)]'
											: 'text-[var(--ds-text-muted)]'
								)}
							>
								{/* Centred on the label's 24px line, above any list. */}
								<span
									className={cn(
										'flex h-6 w-[13px] shrink-0 items-center',
										failed ? 'text-[var(--ds-status-danger)]' : 'text-[var(--ds-status-success)]'
									)}
								>
									{failed ? (
										<Icon name='circle-alert' size={13} />
									) : done ? (
										<Icon className='[stroke-width:2.5]' name='check' size={13} />
									) : null}
								</span>
								<div className='min-w-0 flex-1'>
									<span className='flex min-h-6 items-center'>{step.label}</span>
									{step.items?.length ? <StepItems items={step.items} /> : null}
								</div>
							</li>
						);
					})}
				</ul>
			</div>
		</div>
	);
}

/**
 * The agent at work, then out of the way (Eeva V1 board 4, from A1, A2, B3,
 * G2). Streaming: the Solstice mark turns slowly in the accent beside the
 * thought (15/24 500, no shimmer), the steps collapsed beneath. Done, stopped or failed: one quiet
 * row, the mark in ink and still; click it to open the steps. A step can carry
 * a list (the claims being read); a failed step opens the list so it can't be
 * missed (#1053).
 *
 * @status   draft
 * @variants state: streaming | done | stopped | failed; expanded; step lists; a failed step
 * @use      Above the agent's reply in a turn. In the feed, leave 12px under a collapsed row (A2 pulls it 10px into the 22px feed gap).
 * @avoid    Not a spinner for other loading. The agent's words after a stop or failure go in an `AgentMessage`, with actions after a failure.
 */
export function ThinkingBlock({
	state,
	thought,
	summary,
	steps: stepsProp = [],
	completed: completedProp,
	seconds,
	attempts,
	expanded,
	defaultExpanded = false,
	onExpandedChange,
	className
}: ThinkingBlockProps) {
	const steps = stepsProp.map(step => (typeof step === 'string' ? { label: step } : step));
	const completed = completedProp ?? (state === 'done' ? steps.length : 0);
	const failedStep = steps.some(step => step.status === 'failed');
	// Null until the CEL toggles: a failed step opens the list, and they can still close it.
	const [innerOpen, setInnerOpen] = useState<boolean | null>(null);
	const open = (expanded ?? innerOpen ?? (defaultExpanded || failedStep)) && steps.length > 0;
	// The running step's list sits under the live thought (#1053).
	const current = steps.at(-1);
	const liveItems = current?.label === thought ? current?.items : undefined;
	const label = rowLabel({ state, summary, steps, completed, seconds, attempts });
	const toggle = () => {
		const next = !open;
		if (expanded === undefined) {
			setInnerOpen(next);
		}
		onExpandedChange?.(next);
	};
	const rowButton = 'group rounded-[var(--ds-radius-xs)] outline-none focus-visible:[box-shadow:var(--ds-focus-ring)]';
	const rowText = cn(ROW_TEXT, steps.length > 0 && 'group-hover:text-[var(--ds-text-secondary)]');

	if (state === 'streaming') {
		return (
			<div className={cn('flex flex-col gap-1.5', className)}>
				<div className='flex items-start gap-2'>
					{/* Centred on the thought's first 24px line. */}
					<span className='flex h-6 w-4 shrink-0 items-center justify-center text-[var(--ds-action-accent)]'>
						<SolsticeMark spinning />
					</span>
					<div aria-live='polite' className='min-w-0' role='status'>
						{/* Keyed so each new thought crossfades in (board 8, stream replace). */}
						<span key={thought} className='block animate-in fade-in-0 [animation-duration:var(--ds-dur-fast)]'>
							<span className='text-[length:var(--ds-type-body-size)] font-medium leading-[var(--ds-type-body-leading)] text-[var(--ds-text-primary)]'>
								{thought}
							</span>
						</span>
						{liveItems?.length ? (
							<div className='text-[length:var(--ds-type-ui-size)] text-[var(--ds-text-muted)]'>
								<StepItems items={liveItems} />
							</div>
						) : null}
					</div>
				</div>
				{steps.length ? (
					<div>
						<button
							aria-expanded={open}
							className={cn('ml-6 flex h-6 items-center gap-1.5', rowText, rowButton)}
							type='button'
							onClick={toggle}
						>
							{label}
							<Icon
								className={cn('transition-transform [transition-duration:var(--ds-dur-fast)]', open && 'rotate-180')}
								name='chevron-down'
								size={14}
							/>
						</button>
						<StepList completed={completed} open={open} steps={steps} />
					</div>
				) : null}
			</div>
		);
	}

	const row = (
		<>
			<span className='flex h-6 w-4 shrink-0 items-center justify-center text-[var(--ds-text-primary)]'>
				<SolsticeMark />
			</span>
			<span className={cn('flex h-6 items-center gap-1.5', rowText)}>
				{label}
				{steps.length ? <Icon name={open ? 'chevron-down' : 'chevron-right'} size={14} /> : null}
			</span>
		</>
	);

	return (
		<div className={cn('flex flex-col', className)}>
			{steps.length ? (
				<button
					aria-expanded={open}
					className={cn('flex items-center gap-2 self-start text-left', rowButton)}
					type='button'
					onClick={toggle}
				>
					{row}
				</button>
			) : (
				<div className='flex items-center gap-2'>{row}</div>
			)}
			<StepList completed={completed} open={open} steps={steps} />
		</div>
	);
}
