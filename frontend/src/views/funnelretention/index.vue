<template>
  <section class="page" data-module="funnel-retention">
    <header class="page-head">
      <div>
        <h2>漏斗留存联合视图</h2>
        <p class="page-desc">
          以漏斗为联合入口：步骤事件生成同期群，转化窗口与观察周期共用同一口径版本，
          展示每个群组的步骤转化率、第 1/7/30 天留存与样本不足等异常层级。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新联合视图</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>漏斗编号</span>
        <input v-model="funnelCode" placeholder="如 FUNN-0003，留空查全部" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="funnelCode = ''; reload()">重置条件</button>
    </form>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <article v-for="view in views" :key="view.漏斗.id" class="joint-card">
      <header class="joint-head">
        <div>
          <h3>{{ view.漏斗.漏斗编号 }} · {{ view.漏斗.漏斗名称 }}</h3>
          <p class="page-desc">
            状态：{{ view.漏斗.status }} ｜ 联合同期群：
            <strong>{{ view.同期群?.群组编号 ?? '未生成' }}</strong>
            （{{ view.同期群?.联合状态 ?? '—' }}）
          </p>
        </div>
        <div class="joint-actions">
          <button class="btn" type="button" @click="regenerate(view.漏斗.id)">同版本重新生成</button>
          <button class="btn primary" type="button" @click="openRevise(view)">步骤换版</button>
          <button class="btn ghost" type="button" @click="toggleAudit(view.漏斗.id)">
            {{ auditOpen === view.漏斗.id ? '收起审计' : '查看审计' }}
          </button>
        </div>
      </header>

      <div class="version-line">
        <span class="version-tag">活跃口径版本：{{ view.口径版本 ?? '—' }}</span>
        <span v-for="ver in view.历史版本" :key="ver" class="version-chip" :class="{ active: ver === view.口径版本 }">
          {{ ver }}
        </span>
      </div>
      <p class="source-line">
        事件来源：{{ view.群组结果[0]?.事件来源 ?? '—' }}<br />
        窗口来源：{{ view.群组结果[0]?.窗口来源 ?? '—' }}
      </p>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">群组数</span>
          <strong class="stat-value">{{ view.汇总.群组数 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">完整群组</span>
          <strong class="stat-value">{{ view.汇总.完整群组 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">异常群组</span>
          <strong class="stat-value" :class="{ warn: view.汇总.异常群组 }">{{ view.汇总.异常群组 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">样本不足群组</span>
          <strong class="stat-value" :class="{ warn: view.汇总.样本不足群组 }">{{ view.汇总.样本不足群组 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">平均整体转化率</span>
          <strong class="stat-value">{{ pct(view.汇总.平均整体转化率) }}</strong>
        </article>
      </div>

      <table class="data-table joint-table">
        <thead>
          <tr>
            <th>群组</th>
            <th>粒度</th>
            <th>样本量</th>
            <th v-for="(name, i) in stepNames(view)" :key="name">
              {{ name }}<br /><span class="th-event">{{ view.群组结果[0]?.步骤事件?.[i] }}</span>
            </th>
            <th>整体转化率</th>
            <th v-for="day in checkpoints(view)" :key="day">第 {{ day }} 天留存</th>
            <th>异常标记 / 状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="group in view.群组结果" :key="group.id" :class="{ abnormal: group.异常标记.length }">
            <td>
              {{ group.群组标签 }}
              <div class="muted">{{ group.群组键 }} ｜ 锚点 {{ group.锚点日期 ?? '—' }}</div>
              <div v-if="group.计算状态 === '续算沿用'" class="carry-tag">
                续算自快照 #{{ group.来源快照id }}
              </div>
            </td>
            <td :class="{ nullcell: group.群组粒度 == null }">
              {{ group.群组粒度 ?? '粒度为空' }}
            </td>
            <td>{{ group.样本量 }}</td>
            <td
              v-for="(rate, i) in group.步骤转化率"
              :key="i"
              :class="{ nullcell: rate == null }"
              :title="rate == null ? '该步骤数据缺失或不可信，未用上一个完整结果冒充' : ''"
            >
              {{ pct(rate) }}
            </td>
            <td :class="{ nullcell: group.整体转化率 == null }">{{ pct(group.整体转化率) }}</td>
            <td
              v-for="r in group.留存结果"
              :key="r.检查点天"
              :class="retentionClass(r)"
              :title="r.原因"
            >
              <template v-if="r.留存率 == null">—</template>
              <template v-else>{{ pct(r.留存率) }}</template>
              <div v-if="r.状态 !== '正常'" class="cell-status">{{ r.状态 }}</div>
            </td>
            <td>
              <span v-if="!group.异常标记.length" class="ok-tag">完整</span>
              <ul v-else class="flag-list">
                <li v-for="flag in group.异常标记" :key="flag" class="warn-text">{{ flag }}</li>
              </ul>
            </td>
          </tr>
          <tr v-if="!view.群组结果.length">
            <td :colspan="10" class="empty-state">该漏斗尚未生成联合结果，请先启用漏斗或手动生成</td>
          </tr>
        </tbody>
      </table>

      <div v-if="auditOpen === view.漏斗.id" class="audit-panel">
        <h4>审计记录（事件与窗口来源可追溯）</h4>
        <p v-if="!audits[view.漏斗.id]?.length" class="muted">暂无审计记录</p>
        <table v-else class="data-table">
          <thead>
            <tr><th>时间</th><th>动作</th><th>口径版本</th><th>事件来源</th><th>窗口来源</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="audit in audits[view.漏斗.id]" :key="audit.id">
              <td>{{ audit.时间 }}</td>
              <td>{{ audit.动作 }}</td>
              <td class="mono">{{ audit.口径版本 }}</td>
              <td>{{ audit.事件来源 }}</td>
              <td>{{ audit.窗口来源 }}</td>
              <td>{{ audit.详情 }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>

    <div v-if="!views.length && !errorMessage" class="empty-state page">暂无漏斗留存联合数据</div>

    <div v-if="revisePanel" class="modal-mask" @click.self="revisePanel = null">
      <form class="modal-card" @submit.prevent="submitRevise">
        <h3>步骤换版：{{ revisePanel.漏斗.漏斗编号 }}</h3>
        <p class="page-desc">
          旧版本留存结果会原样保留；仅步骤事件、转化窗口、观察周期变化影响的群组才重算，
          其余群组显式“续算沿用”。
        </p>
        <label class="form-item">
          <span>步骤名称（逗号分隔）</span>
          <input v-model="reviseForm.步骤名称列表" placeholder="注册完成,加入购物车,提交订单" />
        </label>
        <label class="form-item">
          <span>步骤事件（逗号分隔，与步骤一一对应）</span>
          <input v-model="reviseForm.事件定义" placeholder="evt_register,evt_add_cart,evt_order" />
        </label>
        <div class="form-row">
          <label class="form-item">
            <span>转化窗口（天）</span>
            <input v-model.number="reviseForm.转化窗口天" type="number" min="1" />
          </label>
          <label class="form-item">
            <span>观察周期（天）</span>
            <input v-model.number="reviseForm.观察周期天" type="number" min="1" />
          </label>
          <label class="form-item">
            <span>留存检查点（逗号分隔）</span>
            <input v-model="reviseForm.留存检查点天" placeholder="1,7,30" />
          </label>
          <label class="form-item">
            <span>最小样本量</span>
            <input v-model.number="reviseForm.最小样本量" type="number" min="1" />
          </label>
        </div>
        <p v-if="reviseMessage" :class="reviseOk ? 'ok-text' : 'error-text'">{{ reviseMessage }}</p>
        <footer class="modal-foot">
          <button class="btn ghost" type="button" @click="revisePanel = null">取消</button>
          <button class="btn primary" type="submit">提交换版</button>
        </footer>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Retention = {
  检查点天: number
  留存率: number | null
  状态: string
  原因: string
}

type Group = {
  id: number
  群组键: string
  群组标签: string
  群组粒度: string | null
  锚点日期: string | null
  样本量: number
  步骤名称: string[]
  步骤事件: string[]
  步骤转化率: (number | null)[]
  整体转化率: number | null
  留存结果: Retention[]
  异常标记: string[]
  计算状态: string
  来源快照id: number | null
  事件来源: string
  窗口来源: string
}

type JointView = {
  漏斗: {
    id: number
    漏斗编号: string
    漏斗名称: string
    status: string
    转化窗口天?: number
    观察周期天?: number
    最小样本量?: number
  }
  口径版本: string | null
  历史版本: string[]
  同期群: { 群组编号: string; 联合状态: string } | null
  群组结果: Group[]
  汇总: { 群组数: number; 完整群组: number; 异常群组: number; 样本不足群组: number; 平均整体转化率: number | null }
}

type Audit = {
  id: number
  时间: string
  动作: string
  口径版本: string
  事件来源: string
  窗口来源: string
  详情: string
}

const views = ref<JointView[]>([])
const audits = ref<Record<number, Audit[]>>({})
const auditOpen = ref<number | null>(null)
const funnelCode = ref('')
const errorMessage = ref('')
const revisePanel = ref<JointView | null>(null)
const reviseMessage = ref('')
const reviseOk = ref(false)
const reviseForm = ref<Record<string, string | number>>({})

function pct(value: number | null | undefined): string {
  if (value == null) return '—'
  return `${(value * 100).toFixed(2)}%`
}

function stepNames(view: JointView): string[] {
  return view.群组结果[0]?.步骤名称 ?? []
}

function checkpoints(view: JointView): number[] {
  return (view.群组结果[0]?.留存结果 ?? []).map((r) => r.检查点天)
}

function retentionClass(r: Retention): Record<string, boolean> {
  return {
    nullcell: r.留存率 == null,
    warncell: r.留存率 != null && r.状态 !== '正常',
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const query = funnelCode.value.trim() ? `?funnel_code=${encodeURIComponent(funnelCode.value.trim())}` : ''
    const response = await request(`/api/funnel-retention${query}`)
    if (!response.ok) throw new Error('联合视图读取失败')
    const payload = await response.json()
    views.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '联合视图读取失败'
  }
}

async function regenerate(funnelId: number) {
  errorMessage.value = ''
  try {
    const response = await request(`/api/funnel-retention/funnels/${funnelId}/generate`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message ?? '生成失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '联合生成失败'
  }
}

function openRevise(view: JointView) {
  revisePanel.value = view
  reviseMessage.value = ''
  const first = view.群组结果[0]
  reviseForm.value = {
    步骤名称列表: (first?.步骤名称 ?? []).join(','),
    事件定义: (first?.步骤事件 ?? []).join(','),
    转化窗口天: view.漏斗.转化窗口天 ?? 0,
    观察周期天: view.漏斗.观察周期天 ?? 0,
    留存检查点天: checkpoints(view).join(','),
    最小样本量: view.漏斗.最小样本量 ?? 0,
  }
}

function splitList(value: string): string[] {
  return value.split(',').map((part) => part.trim()).filter(Boolean)
}

async function submitRevise() {
  if (!revisePanel.value) return
  reviseMessage.value = ''
  const values = {
    步骤名称列表: splitList(String(reviseForm.value.步骤名称列表 ?? '')),
    事件定义: splitList(String(reviseForm.value.事件定义 ?? '')),
    转化窗口天: Number(reviseForm.value.转化窗口天),
    观察周期天: Number(reviseForm.value.观察周期天),
    留存检查点天: splitList(String(reviseForm.value.留存检查点天 ?? '')).map(Number),
    最小样本量: Number(reviseForm.value.最小样本量),
  }
  try {
    const response = await request(`/api/funnel-retention/funnels/${revisePanel.value.漏斗.id}/revise`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message ?? '换版失败')
    reviseOk.value = true
    reviseMessage.value = payload.message
    revisePanel.value = null
    await reload()
  } catch (error) {
    reviseOk.value = false
    reviseMessage.value = error instanceof Error ? error.message : '换版失败'
  }
}

async function toggleAudit(funnelId: number) {
  if (auditOpen.value === funnelId) {
    auditOpen.value = null
    return
  }
  auditOpen.value = funnelId
  if (!audits.value[funnelId]) {
    try {
      const response = await request(`/api/funnel-retention/funnels/${funnelId}/audits`)
      const payload = await response.json()
      audits.value[funnelId] = payload.items ?? []
    } catch {
      audits.value[funnelId] = []
    }
  }
}

onMounted(reload)
</script>

<style scoped>
.joint-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 18px;
}
.joint-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  flex-wrap: wrap;
}
.joint-head h3 { margin: 0 0 4px; font-size: 16px; }
.joint-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.version-line { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0; }
.version-tag {
  background: #eaf1ff;
  color: var(--brand);
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 12px;
  font-weight: 600;
}
.version-chip {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 2px 8px;
  font-size: 11px;
  color: var(--muted);
}
.version-chip.active { border-color: var(--brand); color: var(--brand); }
.source-line {
  font-size: 12px;
  color: var(--muted);
  background: #f8fafc;
  border: 1px dashed var(--border);
  border-radius: 6px;
  padding: 8px 10px;
  margin: 0 0 10px;
  line-height: 1.7;
}
.joint-table th { vertical-align: top; }
.th-event { color: var(--muted); font-weight: 400; font-size: 11px; }
.nullcell {
  background: #fef3f2;
  color: #b42318;
  text-align: center;
  font-weight: 600;
}
.warncell { background: #fffaeb; }
.warn { color: #b54708; }
.warn-text { color: #b42318; font-size: 12px; }
.ok-text { color: #067647; }
.ok-tag { color: #067647; font-size: 12px; }
.cell-status { font-size: 11px; color: #b54708; margin-top: 2px; }
.flag-list { margin: 0; padding-left: 16px; }
.muted { color: var(--muted); font-size: 11px; }
.carry-tag {
  display: inline-block;
  margin-top: 4px;
  background: #f2f4f7;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
  color: #344054;
}
tr.abnormal td { background: #fffdfa; }
.audit-panel { margin-top: 14px; }
.audit-panel h4 { margin: 0 0 8px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal-card {
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
  width: 560px;
  max-width: 92vw;
}
.modal-card h3 { margin: 0 0 6px; }
.form-item { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; flex: 1; }
.form-item span { font-size: 12px; color: var(--muted); }
.form-item input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px 9px;
  font-size: 13px;
}
.form-row { display: flex; gap: 10px; flex-wrap: wrap; }
.modal-foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
