<template>
  <div>
    <section class="panel">
      <div class="panel-head">
        <h2>联调验收</h2>
        <span class="small muted">一键复现任一场景：基线失败 / 并发抢占 / 迟到批次 / 晚到签约修订 / 规则整期重跑</span>
        <div style="flex:1"></div>
        <button class="btn small danger" :disabled="busy || !canApprove()" @click="reset" title="需要审批员角色">重置演示数据</button>
      </div>
      <div class="panel-body">
        <div class="notice warn small" style="margin-bottom:14px">
          每个场景都会重置并重新播种虚构数据（6 客户 × 3 活动 × 31 天 15 分钟批次），再完整执行一次验收流程。
          审批员角色可触发；只读角色可查看上一次结果。
        </div>
        <div class="grid cols-2">
          <div v-for="s in scenarios" :key="s.key" class="panel">
            <div class="panel-head"><h3>{{ s.title }}</h3></div>
            <div class="panel-body">
              <ol class="small steps" style="padding-left:18px;color:var(--muted)">
                <li v-for="(st, i) in s.steps" :key="i">{{ st }}</li>
              </ol>
              <div style="display:flex;gap:8px;align-items:center;margin-top:10px">
                <button class="btn primary small" :disabled="busy || !canApprove()" @click="runOne(s.key)">
                  {{ busyKey === s.key ? '执行中…' : '回放该场景' }}
                </button>
                <button class="btn small" :disabled="busy" @click="setDetail(s.key)">查看上次结果</button>
              </div>
            </div>
          </div>
        </div>
        <div class="toolbar" style="margin-top:16px">
          <button class="btn primary" :disabled="busy || !canApprove()" @click="runAll">
            {{ busyKey === 'all' ? '全部执行中…' : '运行全部验收场景' }}
          </button>
          <span class="small muted">预计耗时数十秒；包含一次双线程并发抢占。</span>
        </div>
      </div>
    </section>

    <section v-if="report" class="panel" style="margin-top:14px">
      <div class="panel-head">
        <h2>验收结果</h2>
        <span class="badge" :class="report.ok ? 'ok' : 'danger'">{{ report.ok ? '全部通过' : '存在失败' }}</span>
        <div style="flex:1"></div>
        <span class="small muted">{{ report.finished_at || '' }}</span>
      </div>
      <div class="panel-body">
        <div v-for="sc in report.scenarios || [report]" :key="sc.key" class="panel" style="margin-bottom:14px">
          <div class="panel-head">
            <h3>{{ scenarioTitle(sc.key) }}</h3>
            <span class="badge" :class="sc.ok ? 'ok' : 'danger'">{{ sc.ok ? '通过' : '失败' }}</span>
          </div>
          <table>
            <thead><tr><th style="width:36px"></th><th>步骤</th><th>详情</th></tr></thead>
            <tbody>
              <tr v-for="(st, i) in sc.steps" :key="i">
                <td><span :class="st.ok ? 'check' : 'cross'">{{ st.ok ? '✓' : '✗' }}</span></td>
                <td>{{ st.name }}</td>
                <td class="small mono" style="white-space:pre-wrap;word-break:break-all">{{ shortDetail(st.detail) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
    <div v-if="message" class="notice" :class="message.ok ? 'ok' : 'danger'" style="margin-top:14px">{{ message.text }}</div>
    <div v-if="!canApprove()" class="notice info" style="margin-top:14px">当前角色只读或计划员，无法触发验收；请使用审批员账号登录。</div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api, canApprove } from '../api'

const scenarios = ref([])
const report = ref(null)
const busy = ref(false)
const busyKey = ref('')
const message = ref(null)
const detailCache = ref({})

function scenarioTitle(key) {
  return scenarios.value.find((s) => s.key === key)?.title || key
}

function shortDetail(detail) {
  if (detail === undefined || detail === null) return ''
  const text = typeof detail === 'string' ? detail : JSON.stringify(detail)
  return text.length > 420 ? `${text.slice(0, 420)}…` : text
}

async function load() {
  try {
    const data = await api('/demo/scenarios')
    scenarios.value = data.scenarios
    if (data.last_report) {
      report.value = data.last_report.scenarios ? data.last_report : { ok: data.last_report.ok, scenarios: [data.last_report] }
    }
  } catch (e) {
    message.value = { ok: false, text: e.message }
  }
}

async function runOne(key) {
  busy.value = true
  busyKey.value = key
  message.value = null
  try {
    const res = await api('/demo/scenarios/run', { method: 'POST', body: { key } })
    detailCache.value = { ...detailCache.value, [key]: res }
    report.value = { ok: res.ok, scenarios: [res], finished_at: res.finished_at }
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    busy.value = false
    busyKey.value = ''
  }
}

async function runAll() {
  busy.value = true
  busyKey.value = 'all'
  message.value = null
  try {
    report.value = await api('/demo/scenarios/run', { method: 'POST', body: {} })
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    busy.value = false
    busyKey.value = ''
  }
}

function setDetail(key) {
  const cached = detailCache.value[key]
  if (!cached) {
    message.value = { ok: false, text: '本会话中该场景还没有执行记录，请先回放一次。' }
    return
  }
  report.value = { ok: cached.ok, scenarios: [cached], finished_at: cached.finished_at }
}

async function reset() {
  if (!confirm('将清空并重灌演示数据，确定？')) return
  busy.value = true
  try {
    await api('/demo/reset', { method: 'POST' })
    report.value = null
    message.value = { ok: true, text: '演示数据已重置。' }
    await load()
  } catch (e) {
    message.value = { ok: false, text: e.message }
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>
