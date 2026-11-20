import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles.css'
import { auth } from './api'

import LoginView from './views/LoginView.vue'
import OverviewView from './views/OverviewView.vue'
import RegistryView from './views/RegistryView.vue'
import RunsView from './views/RunsView.vue'
import RunDetailView from './views/RunDetailView.vue'
import UncertaintyView from './views/UncertaintyView.vue'
import DiffView from './views/DiffView.vue'
import LedgerView from './views/LedgerView.vue'
import AdjustmentsView from './views/AdjustmentsView.vue'
import AcceptanceView from './views/AcceptanceView.vue'

const routes = [
  { path: '/login', component: LoginView, meta: { public: true } },
  { path: '/', component: OverviewView, meta: { title: '总览' } },
  { path: '/registry', component: RegistryView, meta: { title: '来源登记' } },
  { path: '/runs', component: RunsView, meta: { title: '运行看板' } },
  { path: '/runs/:id', component: RunDetailView, meta: { title: '运行详情' } },
  { path: '/uncertainty', component: UncertaintyView, meta: { title: '基线不确定度' } },
  { path: '/diff', component: DiffView, meta: { title: '差异与追溯' } },
  { path: '/ledger', component: LedgerView, meta: { title: '账本与凭证' } },
  { path: '/adjustments', component: AdjustmentsView, meta: { title: '调整台账' } },
  { path: '/acceptance', component: AcceptanceView, meta: { title: '联调验收' } },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.beforeEach((to) => {
  if (!to.meta.public && !auth.token) return { path: '/login', query: { redirect: to.fullPath } }
  if (to.path === '/login' && auth.token) return { path: '/' }
  return true
})

createApp(App).use(router).mount('#app')
