<template>
  <div>
    <div class="grid cols-2">
      <section class="panel">
        <div class="panel-head"><h2>发起运行</h2><div style="flex:1"></div><span class="small muted">{{ period }}</span></div>
        <div class="panel-body">
          <div class="notice info small" style="margin-bottom:12px">
            每次运行都会生成新版本并冻结输入截止批次与规则快照；
            已发布账本只会被新版本替换并生成差额调整，不会被覆写。
          </div>
          <div class="grid cols-2">
            <div class="field">
              <label>运行策略</label>
              <select v-model="form.strategy">
                <option value="initial">首次 / 指定运行（首次或按活动重跑）</option>
                <option value="targeted">定向重算（按来源影响面）</option>
                <option value="full_period">整期不可变重跑</option>
              </select>
            </div>
            <div class="field" v-if="form.strategy === 'initial'">
              <label>活动范围</label>
              <select v-model="form.event_id">
                <option :value="null">全部活动</option>
                <option v-for="e in events" :key="e.id" :value="e.id">{{ e.code }} · {{ e.name }}</option>
              </select>
            </div>
          </div>

          <div v-if="form.strategy === 'targeted'" class="notice warn small" style="margin-top:12px">
            定向重算需要一个变更来源（补传批次 / 签约版本 / 规则版本）。
            可在「差异与追溯」中直接点「发起定向重算」，或在此粘贴来源 ID。
          </div>
          <div v-if="form.strategy === 'targeted'" class="toolbar" style="margin-top:10px">
            <div class="field" style="flex:1"><label>批次 ID（逗号分隔）</label><input v-model="form.batch_ids" placeholder="例如 233" /></div>
            <div class="field" style="flex:1"><label>签约 ID</label><input v-model="form.contract_ids" placeholder="可空" /></div>
            <div class="field" style="flex:1"><label>规则 ID</label><input v-model="form.rule_ids" placeholder="可空" /></div>
          </div>
          <div v-if="form.strategy !== 'targeted'" class="field" style="margin-top:12px">
            <label>备注</label><input v-model="form.notes" placeholder="本次运行说明" />
          </div>

          <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
            <button class="btn primary" :disabled="!canPlan() || submitting" @click="submit">
              {{ submitting ? '运行中…' : '生成并执行运行' }}
            </button>
            <router-link class="btn" to="/diff">打开差异与追溯</router-link>
          </div>
          <div v-if="!canPlan()" class="small muted" style="margin-top:8px">只读角色不能发起运行</div>
          <div v-if="message" class="notice" :class="message.ok ? 'ok' : 'danger'" style="margin-top:12px">{{ message.text }}</div>

          <details style="margin-top:14px">
            <summary class="small muted" style="cursor:pointer">并发演示：同一客户窗口只能有一个运行取得快照</summary>
            <p class="small muted" style="margin-top:8px">
              打开「联调验收 → 并发窗口抢占」可自动触发两个并发运行并验证仅一个获得 201、另一个 409。
              也可在两个浏览器标签页同时点击「整期不可变重跑」复现。
            </p>
          </details>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head"><h2>运行策略对比</h2><div style="flex:1"></div><span class="small muted">样例规模：6 客户 × 3 活动 = 18 窗口</span></div>
        <div class="panel-body">
          <div class="grid" style="gap:10px">
            <div class="notice info">
              <strong>定向重算</strong>：只重算依赖图上的受影响窗口，其余窗口复用最近一次成功运行的节点结果并保留血缘。
              算力省、等待短，但要求血缘与失效传播准确；适用于补传零星批次、单客户签约修订。
            </div>
            <div class="notice warn">
              <strong>整期不可变重跑</strong>：对所有客户×活动窗口重新计算，一致性核对最简单，
              但算力与等待随样例规模线性增长；适用于规则修订、批量补传或对账前的兜底。
            </div>
          </div>
          <table style="margin-top:6px">
            <thead><tr><th>策略</th><th class="num">运行次数</th><th class="num">平均耗时</th><th class="num">重算窗口</th><th class="num">复用窗口</th></tr></thead>
            <tbody>
              <tr v-for="s in stats" :key="s.strategy">
                <td>{{ STRATEGY_LABEL[s.strategy] || s.strategy }}</td>
                <td class="num">{{ s.runs }}</td>
                <td class="num">{{ (s.avg_duration_ms / 1000).toFixed(2) }} s</td>
                <td class="num">{{ s.computed_windows }}</td>
                <td class="num">{{ s.reused_windows }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="!stats.length" class="empty">暂无运行数据</div>
        </div>
      </section>
    </div>

    <section class="panel" style="margin-top:14px">
      <div class="panel-head">
        <h2>运行列表</h2><div style="flex:1"></div>
        <select v-model="filterStatus" style="width:150px">
          <option value="">全部状态</option>
          <option value="success">成功</option>
          <option value="needs_review">待核</option>
          <option value="failed">失败</option>
        </select>
        <button class="btn small" @click="load" :disabled="loading">刷新</button>
      </div>
      <div v-if="error" class="panel-body"><div class="notice danger">{{ error }}</div></div>
      <table v-else>
        <thead>
          <tr><th>运行</th><th>策略</th><th>状态</th><th class="num">窗口 重算/复用</th><th class="num">节点</th><th>输入截止</th><th>发布</th><th>耗时</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in filtered" :key="r.id" class="clickable" @click="$router.push(`/runs/${r.id}`)">
            <td class="mono">{{ r.run_code }}<div class="small muted">v{{ r.version }} · {{ r.created_by }}</div></td>
            <td>{{ STRATEGY_LABEL[r.strategy] }}<div class="small muted" v-if="r.base_run_id">基线 #{{ r.base_run_id }}</div></td>
            <td><StatusBadge :status="r.status" /><div class="small muted" v-if="r.window_counts?.needs_review">{{ r.window_counts.needs_review }} 窗口待核</div></td>
            <td class="num">{{ r.target_windows }} / {{ r.computed_windows }} / {{ r.reused_windows }}</td>
            <td class="num">{{ r.node_count }}</td>
            <td class="small">{{ fmt.dt(r.cutoff_at) }}</td>
            <td>
              <span v-if="r.published_at" class="badge info">已发布</span>
              <button v-else class="btn small" :disabled="!canApprove() || r.status !== 'success' || publishing[r.id]" @click.stop="publish(r)">
                {{ publishing[r.id] ? '发布中…' : '发布' }}
              </button>
            </td>
            <td class="small">{{ r.duration_ms }} ms</td>
            <td><router-link :to="`/runs/${r.id}`" @click.stop>详情</router-link></td>
          </tr>
        </tbody>
      </table>
      <div v-if="!filtered.length" class="empty">暂无运行</div>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, canApprove, canPlan, fmt, STRATEGY_LABEL } from '../api'
import { periodStore } from '../periodStore'
import StatusBadge from '../components/StatusBadge.vue'

const period = computed(() => periodStore.period)
const events = ref([])
const runs = ref([])
const stats = ref([])
const loading = ref(false)
const submitting = ref(false)
const publishing = ref({})
const error = ref('')
const message = ref(null)
const filterStatus = ref('')
const form = ref({ strategy: 'initial', event_id: null, notes: '', batch_ids: '', contract_ids: '', rule_ids: '' })

const filtered = computed(() => (filterStatus.value ? runs.value.filter((r) => r.status === filterStatus.value) : runs.value))

function parseIds(text) {
  if (!text) return null
  const ids = String(text)
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)
    .map(Number)
    .filter((n) => Number.isFinite(n))
  return ids.length ? ids : null
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [r, s, e] = await Promise.all([
      api('/runs', { params: { period_month: period.value } }),
      api('/overview', { params: { period_month: period.value } }),
      api('/events', { params: { period_month: period.value } }),
    ])
    runs.value = r
    stats.value = s.strategy_stats || []
    events.value = e
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

async function submit() {
  submitting.value = true
  message.value = null
  try {
    const body = { period_month: period.value, strategy: form.value.strategy, notes: form.value.notes }
    if (form.value.strategy === 'initial' && form.value.event_id) body.event_id = Number(form.value.event_id)
    if (form.value.strategy === 'targeted') {
      const b = parseIds(form.value.batch_ids)
      const c = parseIds(form.value.contract_ids)
      const r = parseIds(form.value.rule_ids)
      if (!b && !c && !r) {
        message.value = { ok: false, text: '定向重算需至少提供一个批次 / 签约 / 规则 ID' }
        submitting.value = false
        return
      }
      if (b) body.batch_ids = b
      if (c) body.contract_ids = c
      if (r) body.rule_ids = r
    }
    const run = await api('/runs', { method: 'POST', body })
    message.value = {
      ok: true,
      text: `运行 ${run.run_code} 已完成：状态 ${run.status}，重算 ${run.computed_windows} / 复用 ${run.reused_windows} 窗口。`,
    }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    submitting.value = false
  }
}

async function publish(r) {
  publishing.value = { ...publishing.value, [r.id]: true }
  try {
    const res = await api(`/runs/${r.id}/publish`, { method: 'POST', body: { reason: `发布 ${r.run_code}` } })
    message.value = {
      ok: true,
      text: `已发布 ${res.ledger_entry_ids.length} 条账本，生成 ${res.adjustment_ids.length} 笔调整（合计 ${fmt.money(res.adjustment_total)}）。`,
    }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    publishing.value = { ...publishing.value, [r.id]: false }
  }
}

onMounted(load)
</script>
