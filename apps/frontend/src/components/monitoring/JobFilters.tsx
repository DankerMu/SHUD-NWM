import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import type { JobStatus } from '@/lib/constants'
import type { JobFilters as JobFilterState } from '@/stores/monitoring'

interface JobFiltersProps {
  filters: JobFilterState
  onChange: (filters: JobFilterState) => void
}

const statusOptions: Array<{ value: JobStatus; label: string }> = [
  { value: 'pending', label: 'pending' },
  { value: 'submitted', label: 'submitted' },
  { value: 'running', label: 'running' },
  { value: 'succeeded', label: 'succeeded' },
  { value: 'failed', label: 'failed' },
  { value: 'cancelled', label: 'cancelled' },
  { value: 'submission_failed', label: 'submission_failed' },
  { value: 'permanently_failed', label: 'permanently_failed' },
  { value: 'partially_failed', label: 'partially_failed' },
]

const runTypeOptions = ['forecast', 'analysis', 'hindcast']

const scenarioOptions = [
  { value: 'forecast_gfs_deterministic', label: 'GFS' },
  { value: 'forecast_ifs_deterministic', label: 'IFS' },
  { value: 'analysis_true_field', label: 'analysis_true_field' },
]

function nextValue(value: string) {
  return value === 'all' ? undefined : value
}

export function JobFilters({ filters, onChange }: JobFiltersProps) {
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <Select
        value={filters.status ?? 'all'}
        onValueChange={(value) => onChange({ ...filters, status: nextValue(value) as JobStatus | undefined })}
      >
        <SelectTrigger aria-label="Status filter">
          <SelectValue placeholder="全部状态" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">全部状态</SelectItem>
          {statusOptions.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={filters.runType ?? 'all'}
        onValueChange={(value) => onChange({ ...filters, runType: nextValue(value) })}
      >
        <SelectTrigger aria-label="Run type filter">
          <SelectValue placeholder="全部类型" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">全部类型</SelectItem>
          {runTypeOptions.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={filters.scenario ?? 'all'}
        onValueChange={(value) => onChange({ ...filters, scenario: nextValue(value) })}
      >
        <SelectTrigger aria-label="Scenario filter">
          <SelectValue placeholder="全部场景" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">全部场景</SelectItem>
          {scenarioOptions.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}
