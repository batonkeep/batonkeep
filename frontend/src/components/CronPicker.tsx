// CronPicker.tsx — dual-mode scheduling UX (D-0025).
// Primary mode: friendly preset selector with time/day controls.
// Escape hatch: "Custom / Advanced" toggle reveals the raw cron input.
// Cron is always canonical — the backend receives the same string as before.
import { useState, useEffect } from "react";
import { ChevronDown, ChevronUp, Clock } from "lucide-react";
import {
  type CronPresetId,
  CRON_PRESET_LABELS,
  DAYS_OF_WEEK,
  buildCronFromPreset,
  cronToPreset,
  isValidCron,
} from "../format";
import { Field, Input, Select } from "../ui";

interface Props {
  /** Current cron expression (controlled). */
  value: string;
  /** Called whenever the cron string changes (from picker or raw input). */
  onChange: (cron: string) => void;
  /** Whether the current value is invalid (drives border colour). */
  hasError?: boolean;
}

// Hours 0–23 as option list.
const HOURS = Array.from({ length: 24 }, (_, i) => ({
  value: String(i),
  label: String(i).padStart(2, "0") + ":00",
}));

// Common minute values.
const MINUTES = [0, 5, 10, 15, 20, 30, 45].map((m) => ({
  value: String(m),
  label: String(m).padStart(2, "0"),
}));

// Day-of-month 1–28 (safe for all months).
const DAYS_OF_MONTH = Array.from({ length: 28 }, (_, i) => ({
  value: String(i + 1),
  label: String(i + 1),
}));

const PRESET_ORDER: CronPresetId[] = [
  "hourly",
  "daily",
  "weekdays",
  "weekly",
  "monthly",
  "custom",
];

export default function CronPicker({ value, onChange, hasError }: Props) {
  // Parse the incoming cron string to derive picker state.
  const parsed = cronToPreset(value || "0 9 * * *");
  const [preset, setPreset] = useState<CronPresetId>(parsed.preset);
  const [hour, setHour] = useState(parsed.hour);
  const [minute, setMinute] = useState(parsed.minute);
  const [dayOfWeek, setDayOfWeek] = useState(parsed.dayOfWeek);
  const [dayOfMonth, setDayOfMonth] = useState(parsed.dayOfMonth);
  const [showAdvanced, setShowAdvanced] = useState(parsed.preset === "custom");
  const [rawCron, setRawCron] = useState(value || "");

  // Keep rawCron in sync when the parent changes value externally.
  useEffect(() => {
    setRawCron(value || "");
    const p = cronToPreset(value || "");
    setPreset(p.preset);
    setHour(p.hour);
    setMinute(p.minute);
    setDayOfWeek(p.dayOfWeek);
    setDayOfMonth(p.dayOfMonth);
    if (p.preset === "custom") setShowAdvanced(true);
  }, [value]);

  // Emit a built cron when picker controls change.
  const emitBuilt = (
    p: CronPresetId,
    h: number,
    m: number,
    dow: number,
    dom: number
  ) => {
    if (p === "custom") return; // raw input handles its own emit
    const cron = buildCronFromPreset(p, h, m, dow, dom);
    if (cron) onChange(cron);
  };

  const handlePresetChange = (id: CronPresetId) => {
    setPreset(id);
    if (id === "custom") {
      setShowAdvanced(true);
    } else {
      setShowAdvanced(false);
      emitBuilt(id, hour, minute, dayOfWeek, dayOfMonth);
    }
  };

  const handleHour = (h: number) => {
    setHour(h);
    emitBuilt(preset, h, minute, dayOfWeek, dayOfMonth);
  };
  const handleMinute = (m: number) => {
    setMinute(m);
    emitBuilt(preset, hour, m, dayOfWeek, dayOfMonth);
  };
  const handleDow = (d: number) => {
    setDayOfWeek(d);
    emitBuilt(preset, hour, minute, d, dayOfMonth);
  };
  const handleDom = (d: number) => {
    setDayOfMonth(d);
    emitBuilt(preset, hour, minute, dayOfWeek, d);
  };

  const handleRawChange = (expr: string) => {
    setRawCron(expr);
    onChange(expr);
    // Try to re-sync picker state from the raw input.
    if (isValidCron(expr)) {
      const p = cronToPreset(expr);
      setPreset(p.preset);
      setHour(p.hour);
      setMinute(p.minute);
      setDayOfWeek(p.dayOfWeek);
      setDayOfMonth(p.dayOfMonth);
    }
  };

  const needsTime = preset !== "hourly" && preset !== "custom";
  const needsDow = preset === "weekly";
  const needsDom = preset === "monthly";

  // Computed cron preview (shown alongside the friendly picker).
  const previewCron =
    preset === "custom"
      ? rawCron
      : buildCronFromPreset(preset, hour, minute, dayOfWeek, dayOfMonth);

  return (
    <div className="space-y-3">
      {/* Preset selector */}
      <Field label="Schedule preset">
        <Select
          value={preset}
          onChange={(e) => handlePresetChange(e.target.value as CronPresetId)}
        >
          {PRESET_ORDER.map((id) => (
            <option key={id} value={id}>
              {CRON_PRESET_LABELS[id]}
            </option>
          ))}
        </Select>
      </Field>

      {/* Contextual time / day controls */}
      {preset !== "custom" && (
        <div className="flex flex-wrap gap-3">
          {/* Minute (hourly only) */}
          {preset === "hourly" && (
            <Field label="At minute">
              <Select
                value={String(minute)}
                onChange={(e) => handleMinute(parseInt(e.target.value, 10))}
              >
                {MINUTES.map((m) => (
                  <option key={m.value} value={m.value}>
                    :{m.label}
                  </option>
                ))}
              </Select>
            </Field>
          )}

          {/* Hour + minute (non-hourly presets) */}
          {needsTime && (
            <>
              <Field label="Hour">
                <Select
                  value={String(hour)}
                  onChange={(e) => handleHour(parseInt(e.target.value, 10))}
                >
                  {HOURS.map((h) => (
                    <option key={h.value} value={h.value}>
                      {h.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Minute">
                <Select
                  value={String(minute)}
                  onChange={(e) => handleMinute(parseInt(e.target.value, 10))}
                >
                  {MINUTES.map((m) => (
                    <option key={m.value} value={m.value}>
                      :{m.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </>
          )}

          {/* Day of week (weekly) */}
          {needsDow && (
            <Field label="Day of week">
              <Select
                value={String(dayOfWeek)}
                onChange={(e) => handleDow(parseInt(e.target.value, 10))}
              >
                {DAYS_OF_WEEK.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </Select>
            </Field>
          )}

          {/* Day of month (monthly) */}
          {needsDom && (
            <Field label="Day of month">
              <Select
                value={String(dayOfMonth)}
                onChange={(e) => handleDom(parseInt(e.target.value, 10))}
              >
                {DAYS_OF_MONTH.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </Select>
            </Field>
          )}
        </div>
      )}

      {/* Cron preview / advanced toggle */}
      <div className="flex items-center justify-between gap-3">
        {preset !== "custom" && previewCron && (
          <div className="flex items-center gap-1.5 rounded-md border border-edge bg-panel px-2.5 py-1.5">
            <Clock size={11} className="shrink-0 text-muted" />
            <span className="font-mono text-[11px] text-muted">
              cron:{" "}
              <span className="text-ink">{previewCron}</span>
            </span>
          </div>
        )}
        <button
          type="button"
          onClick={() => {
            if (preset !== "custom") {
              // Entering advanced from a preset — keep the generated cron in the raw field.
              setRawCron(previewCron);
            }
            setShowAdvanced((s) => !s);
          }}
          className="ml-auto flex items-center gap-1 text-[11px] text-muted hover:text-ink"
        >
          {showAdvanced && preset !== "custom" ? (
            <>
              <ChevronUp size={12} /> Hide advanced
            </>
          ) : (
            <>
              <ChevronDown size={12} /> Custom / Advanced
            </>
          )}
        </button>
      </div>

      {/* Raw cron input (escape hatch) */}
      {(showAdvanced || preset === "custom") && (
        <Field
          label="Crontab expression (min hour dom mon dow)"
          hint="5-field standard cron. Changes here update the picker when the pattern is recognised."
        >
          <Input
            value={rawCron}
            onChange={(e) => handleRawChange(e.target.value)}
            placeholder="0 9 * * 1"
            className={hasError ? "border-bad" : ""}
          />
        </Field>
      )}
    </div>
  );
}
