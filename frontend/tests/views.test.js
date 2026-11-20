import { describe, expect, it, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import StatusBadge from '../src/components/StatusBadge.vue'
import OverviewView from '../src/views/OverviewView.vue'
import RunsView from '../src/views/RunsView.vue'
import UncertaintyView from '../src/views/UncertaintyView.vue'

vi.mock('../src/api', async () => {
  const actual = await vi.importActual('../src/api')
  return {
    ...actual,
    api: vi.fn(async (path) => {
      if (path === '/overview') {
        return {
          run_total: 3,
          run_needs_review: 1,
          run_failed: 0,
          ledger_published_amount: 22125.99,
          ledger_published_count: 18,
          adjustment_pending_count: 2,
          adjustment_pending_delta: 120.5,
          adjustment_approved_delta: 0,
          recent_runs: [
            { id: 1, run_code: 'RUN-1', strategy: 'initial', status: 'success', target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 90, cutoff_at: '2025-09-01T00:00:00Z', published_at: '2025-09-01T01:00:00Z' },
          ],
          ledger_by_period: [{ period_month: '2025-08', amount: 22125.99, entry_count: 18 }],
          strategy_stats: [{ strategy: 'targeted', runs: 1, avg_duration_ms: 800, computed_windows: 1, reused_windows: 17 }],
        }
      }
      if (path === '/runs') {
        return [
          { id: 1, run_code: 'RUN-1', strategy: 'initial', status: 'success', version: 1, target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 90, duration_ms: 800, cutoff_at: '2025-09-01T00:00:00Z', published_at: '2025-09-01T01:00:00Z', window_counts: { success: 18 } },
          { id: 2, run_code: 'RUN-2', strategy: 'full_period', status: 'needs_review', version: 2, target_windows: 18, computed_windows: 18, reused_windows: 0, node_count: 90, duration_ms: 900, cutoff_at: '2025-09-02T00:00:00Z', published_at: null, window_counts: { needs_review: 18 } },
        ]
      }
      if (path === '/runs/1/uncertainty') {
        return {
          run: { id: 1, run_code: 'RUN-1', cutoff_at: '2025-09-01T00:00:00Z' },
          reports: [
            { id: 11, run_id: 1, customer_code: 'C-001', customer_name: '滨江化工有限公司', event_code: 'DR-20250805', event_date: '2025-08-05', status: 'ok', level: 0.9, baseline_kwh: 943.1, lower_kwh: 931.2, upper_kwh: 958.3, half_width_kwh: 13.5, relative_width: 0.014, qualified_days: 11, pseudo_days: 8, weekday_covered: false, weather_status: 'ok', degraded_reasons: [], unavailable_reasons: [] },
            { id: 12, run_id: 1, customer_code: 'C-003', customer_name: '云岭水泥厂', event_code: 'DR-20250812', event_date: '2025-08-12', status: 'unavailable', level: 0.9, baseline_kwh: 812.4, lower_kwh: null, upper_kwh: null, half_width_kwh: null, relative_width: null, qualified_days: 2, pseudo_days: 1, weekday_covered: true, weather_status: 'missing', degraded_reasons: [], unavailable_reasons: ['qualified_samples_insufficient'] },
          ],
        }
      }
      if (path === '/uncertainty/11') {
        return {
          id: 11,
          customer_code: 'C-001',
          event_code: 'DR-20250805',
          event_date: '2025-08-05',
          status: 'ok',
          level: 0.9,
          baseline_kwh: 943.1,
          degraded_reasons: [],
          unavailable_reasons: [],
          run_cutoff_at: '2025-09-01T00:00:00Z',
          interval: { lower_kwh: 931.2, upper_kwh: 958.3, half_width_kwh: 13.5, relative_width: 0.014 },
          report: {
            disclaimer: '区间为基线不确定度报告：不修改基线数值、不宣称实际削减量',
            interval: { lower_kwh: 931.2, upper_kwh: 958.3, half_width_kwh: 13.5, relative_width: 0.014 },
            width_components: { residual_noise_kwh: 9.75, sample_size_factor: 1.0954, coverage_factor: 1.265, weekday_factor: 1.15, weather_factor: 1.1, final_half_width_kwh: 13.5, notes: ['残差噪声'] },
            methods: {
              holdout: { status: 'ok', fit: { n: 5, bias_kwh: 0.4, std_kwh: 5.1, lag1_autocorr: 0.12 }, holdout: { n: 3, empirical_coverage: 1.0, days: [{ day: '2025-08-04', actual_kwh: 940.1, interval: [930.0, 955.0], covered: true }], representative: true, temp_mean_c: 31.2, event_temp_c: 32.0 } },
              block_bootstrap: { status: 'ok', warnings: [], pool: { n_days: 8, n_blocks: 6, block_length: 3, similarity: 'weekday+weather' }, replicates: 200, half_width_kwh: { p10: 8.9, p50: 10.1, p90: 12.4 }, season_drift: { drift_ratio: 0.05, threshold: 0.15 } },
            },
            samples: {
              calibration_window: { start: '2025-07-08', end: '2025-08-04', days: 28 },
              qualified_days: 11,
              pseudo_days: 8,
              baseline_sample_days: 5,
              weekday_coverage: { event_weekday: 1, sample_weekdays: [0, 3, 4, 5, 6], event_weekday_in_samples: false },
              weather_coverage: { status: 'ok', event_temp_c: 32.0, sample_temp_min_c: 29.1, sample_temp_max_c: 33.4, missing_weather_days: [] },
              eligibility: [
                { day: '2025-08-04', weekday: 0, eligible: true, reasons: [], reason_labels: [], role: 'calibration', day_total_kwh: 940.1, temp_c: 31.5, pseudo: { day: '2025-08-04', weekday: 0, temp_c: 31.5, actual_kwh: 940.1, predicted_kwh: 941.0, residual_kwh: -0.9, sample_days: 5, excluded_days: ['2025-08-04'], leakage_window: ['2025-08-04', '2025-08-05'] } },
                { day: '2025-08-05', weekday: 1, eligible: false, reasons: ['event_day'], reason_labels: ['需求响应活动日'], role: 'calibration', day_total_kwh: 800.2, temp_c: 32.0 },
              ],
            },
          },
          input_refs: { rule_id: 1, rule_version: 1, cutoff_at: '2025-09-01T00:00:00Z', weather_ids: [1, 2], holiday_ids: [1], outage_ids: [], batch_ids: [10, 11], params: { calibration_lookback_days: 28, min_residuals: 5, block_length: 3, bootstrap_replicates: 200 } },
        }
      }
      if (path === '/events') return []
      throw new Error(`unexpected path ${path}`)
    }),
  }
})

beforeEach(() => {
  globalThis.fetch = vi.fn()
})

describe('StatusBadge', () => {
  it('maps status to Chinese label and class', () => {
    const ok = mount(StatusBadge, { props: { status: 'success' } })
    expect(ok.text()).toBe('成功')
    expect(ok.classes()).toContain('ok')
    const missing = mount(StatusBadge, { props: { status: 'needs_review' } })
    expect(missing.text()).toBe('待核')
    expect(missing.classes()).toContain('warn')
    const blocked = mount(StatusBadge, { props: { status: 'blocked' } })
    expect(blocked.classes()).toContain('danger')
  })
})

describe('OverviewView', () => {
  it('renders published total and pending adjustment delta', async () => {
    const wrapper = mount(OverviewView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('已发布账本合计')
    expect(text).toContain('22,125.99')
    expect(text).toContain('120.50')
    expect(text).toContain('RUN-1')
  })
})

describe('RunsView', () => {
  it('shows run list with statuses and disables publish for non-success', async () => {
    const wrapper = mount(RunsView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('RUN-1')
    expect(text).toContain('RUN-2')
    expect(text).toContain('整期重跑')
    expect(text).toContain('待核')
  })
})

describe('UncertaintyView', () => {
  it('lists window intervals with status badges and coverage chips', async () => {
    const wrapper = mount(UncertaintyView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('基线不确定度分析')
    expect(text).toContain('C-001')
    expect(text).toContain('充分')
    expect(text).toContain('无法估计')
    expect(text).toContain('[931.2, 958.3]')
    expect(text).toContain('天气缺项')
  })

  it('opens detail with width decomposition, residuals and diagnostics', async () => {
    const wrapper = mount(UncertaintyView, { global: { stubs: { RouterLink: true } } })
    await new Promise((r) => setTimeout(r, 0))
    await new Promise((r) => setTimeout(r, 0))
    const row = wrapper.findAll('tbody tr').at(0)
    await row.trigger('click')
    await new Promise((r) => setTimeout(r, 0))
    const text = wrapper.text()
    expect(text).toContain('区间宽度来源')
    expect(text).toContain('残差噪声')
    expect(text).toContain('9.75')
    expect(text).toContain('伪活动日预测残差')
    expect(text).toContain('2025-08-04')
    expect(text).toContain('留出交叉拟合')
    expect(text).toContain('连续日期块重采样')
    expect(text).toContain('样本资格与排除理由')
    expect(text).toContain('需求响应活动日')
    expect(text).toContain('不修改基线数值')
  })
})
