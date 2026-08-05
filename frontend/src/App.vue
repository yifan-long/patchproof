<template>
  <div class="shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">EVIDENCE-FIRST CODING AGENT HARNESS</p>
        <h1>PatchProof</h1>
        <p class="subtitle">Agent 只有在可回放证据、测试和人工确认都成立时，才有资格声称完成。</p>
      </div>
      <div class="health" :class="healthOk ? 'ok' : 'pending'">{{ healthText }}</div>
    </header>

    <main class="layout">
      <section class="card composer">
        <div class="card-heading">
          <div>
            <p class="label">NEW TASK</p>
            <h2>提交仓库维护任务</h2>
          </div>
          <span class="badge">typed loop · SQLite</span>
        </div>
        <label>
          目标仓库
          <input v-model="form.repo_path" type="text" />
        </label>
        <label>
          任务目标
          <textarea v-model="form.goal" rows="5" placeholder="例如：修复 ChatService 缺失字段时的异常处理，并补一条回归测试。" />
        </label>
        <div class="two-col">
          <label>
            检查命令
            <input v-model="form.check_command" type="text" />
          </label>
          <label>
            修复轮数
            <input v-model.number="form.max_iterations" type="number" min="1" max="20" />
          </label>
        </div>
        <label>
          最大 tool steps
          <input v-model.number="form.max_steps" type="number" min="1" max="200" />
        </label>
        <details class="provider-settings">
          <summary>LLM Provider（可选，自带 API key）</summary>
          <label>
            Base URL
            <input v-model="form.provider.base_url" type="text" placeholder="https://opencode.ai/zen/go/v1 或自定义端点" />
          </label>
          <label>
            Model
            <input v-model="form.provider.model" type="text" placeholder="deepseek-v4-flash / gpt-4o / ..." />
          </label>
          <label>
            API Key
            <input v-model="form.provider.api_key" type="password" placeholder="仅本次任务使用，不落库" />
          </label>
          <label>
            Transport
            <select v-model="form.provider.transport">
              <option value="auto">auto（按模型推断）</option>
              <option value="openai-compatible">openai-compatible</option>
              <option value="anthropic-compatible">anthropic-compatible</option>
            </select>
          </label>
          <p class="hint">不填则使用服务器默认 Provider。密钥只在浏览器本地保存，随任务提交后仅存于内存。</p>
        </details>
        <button class="primary" :disabled="busy || !form.goal.trim()" @click="createTask">
          {{ busy ? '任务运行中…' : '启动 Evidence Loop' }}
        </button>
        <p class="hint">Agent 只在 worktree/snapshot 中修改；测试通过后生成 Receipt，仍需人工 Apply。</p>
      </section>

      <section class="card history-card">
        <div class="card-heading">
          <div>
            <p class="label">DURABLE TASKS</p>
            <h2>历史任务</h2>
          </div>
          <button class="ghost small-button" @click="loadTasks">刷新</button>
        </div>
        <div v-if="history.length" class="history-list">
          <button
            v-for="task in history"
            :key="task.id"
            class="history-item"
            :class="task.id === currentTask?.id ? 'selected' : ''"
            @click="selectTask(task)"
          >
            <span class="history-status" :class="task.status"></span>
            <span>
              <strong>{{ task.goal }}</strong>
              <small>#{{ task.id }} · {{ task.status }} · {{ formatTime(task.updated_at) }}</small>
            </span>
          </button>
        </div>
        <p v-else class="empty">SQLite 中还没有历史任务。</p>
      </section>

      <section class="card status-card">
        <div class="card-heading">
          <div>
            <p class="label">STATE MACHINE</p>
            <h2>{{ currentTask ? currentTask.status : '等待任务' }}</h2>
          </div>
          <button v-if="currentTask && !terminal" class="ghost" @click="cancelTask">取消</button>
        </div>
        <div v-if="currentTask" class="task-meta">
          <span>#{{ currentTask.id }}</span>
          <span>iteration {{ currentTask.iteration }}/{{ currentTask.max_iterations }}</span>
          <span>steps {{ currentTask.budget_used }}/{{ currentTask.max_steps }}</span>
          <span>{{ currentTask.workspace_kind || 'workspace pending' }}</span>
          <span>required check {{ currentTask.required_check_evidence_valid ? 'verified' : 'pending' }}</span>
        </div>
        <div v-if="currentTask?.workspace_reason" class="workspace-note">{{ currentTask.workspace_reason }}</div>
        <div v-if="currentTask?.pending_command" class="approval-box danger">
          <strong>命令需要人工审批</strong>
          <code>{{ currentTask.pending_command.join(' ') }}</code>
          <span class="risk">RISK {{ currentTask.pending_risk }} · {{ currentTask.pending_reason }}</span>
          <div class="actions">
            <button class="danger-button" @click="approveCommand(false)">拒绝</button>
            <button class="primary small" @click="approveCommand(true)">批准执行</button>
          </div>
        </div>
        <div v-if="currentTask?.status === 'awaiting_apply'" class="approval-box">
          <strong>证据链成立，等待写回</strong>
          <p>先检查 diff 和 Receipt，再确认 Apply 到真实仓库。</p>
          <button class="primary" @click="applyTask">确认 Apply 到真实仓库</button>
        </div>
        <div class="event-list">
          <div v-for="event in events" :key="event.seq" class="event">
            <span class="event-dot" :class="event.stage"></span>
            <div>
              <strong>#{{ event.seq }} · {{ event.message }}</strong>
              <small>{{ event.stage }} · {{ formatTime(event.ts) }} · {{ shortHash(event.event_hash) }}</small>
              <pre v-if="event.data?.stderr || event.data?.stdout" class="log">{{ event.data.stdout }}{{ event.data.stderr }}</pre>
            </div>
          </div>
          <p v-if="!events.length" class="empty">选择历史任务或启动新任务，这里会显示完整状态和 tool 轨迹。</p>
        </div>
      </section>

      <section class="card diff-card">
        <div class="card-heading">
          <div>
            <p class="label">PROOF OF CHANGE</p>
            <h2>隔离修改</h2>
          </div>
          <span class="badge">{{ currentTask?.changed_files?.length || 0 }} files</span>
        </div>
        <div v-if="currentTask?.changed_files?.length" class="file-list">
          <span v-for="file in currentTask.changed_files" :key="file">{{ file }}</span>
        </div>
        <pre v-if="currentTask?.diff" class="diff">{{ currentTask.diff }}</pre>
        <p v-else class="empty">暂无 diff。Agent 需要通过 typed apply_edit 产生修改。</p>
      </section>

      <section class="card result-card">
        <div class="card-heading">
          <div>
            <p class="label">PATCH RECEIPT</p>
            <h2>完成证据</h2>
          </div>
          <span v-if="receipt" class="result-chip" :class="receiptVerified ? 'pass' : 'fail'">
            {{ receiptVerified ? 'VERIFIED' : 'TAMPERED' }}
          </span>
        </div>
        <div v-if="receipt" class="receipt-grid">
          <div><small>receipt hash</small><code>{{ receipt.receipt_hash }}</code></div>
          <div><small>event chain head</small><code>{{ receipt.receipt.event_chain_head }}</code></div>
          <div><small>verdict</small><strong>{{ receipt.receipt.verdict }}</strong></div>
          <div><small>workspace</small><strong>{{ receipt.receipt.workspace.kind }}</strong></div>
          <div><small>tests</small><strong>{{ receipt.receipt.tests.passed ? 'PASS' : 'FAIL' }}</strong></div>
          <div><small>approvals</small><strong>{{ receipt.receipt.approvals?.length || 0 }}</strong></div>
          <div><small>receipt file</small><strong>{{ receipt.file_verified ? 'HASH OK' : 'MISSING/TAMPERED' }}</strong></div>
          <div><small>required check</small><strong>{{ receipt.receipt.tests.required_check?.verified ? 'PASS' : 'FAIL' }}</strong></div>
        </div>
        <pre v-if="receipt" class="log receipt-json">{{ JSON.stringify(receipt.receipt, null, 2) }}</pre>
        <p v-else class="empty">测试通过并执行 finish(verified) 后，这里会出现可验证 Receipt。</p>
      </section>

      <section class="card result-card">
        <div class="card-heading">
          <div>
            <p class="label">TEST EVIDENCE</p>
            <h2>最近一次验证</h2>
          </div>
          <span v-if="currentTask?.test_result" class="result-chip" :class="currentTask.test_result.returncode === 0 ? 'pass' : 'fail'">
            {{ currentTask.test_result.returncode === 0 ? 'PASS' : 'FAIL' }}
          </span>
        </div>
        <pre v-if="currentTask?.test_result" class="log large">{{ currentTask.test_result.stdout }}{{ currentTask.test_result.stderr }}</pre>
        <p v-else class="empty">还没有 run_check 证据。</p>
      </section>

      <section class="card evaluation-card">
        <div class="card-heading">
          <div>
            <p class="label">EVALUATION CORPUS</p>
            <h2>Corpus &amp; readiness</h2>
          </div>
          <button class="ghost small-button" @click="loadEvaluation">刷新预检</button>
        </div>
        <div class="metric-grid">
          <div><small>provider</small><strong>{{ healthData?.provider?.source || 'configured' }}</strong></div>
          <div><small>model</small><strong>{{ healthData?.provider?.model || 'not configured' }}</strong></div>
          <div><small>first pass</small><strong>${{ healthData?.evaluation?.first_pass_budget_usd ?? '—' }}</strong></div>
          <div><small>expansion</small><strong>${{ healthData?.evaluation?.expansion_budget_usd ?? '—' }}</strong></div>
        </div>
        <div v-if="preflight" class="readiness-grid">
          <div class="readiness" :class="preflight.docker.execution_mode === 'docker_isolated' ? 'ready' : 'blocked'">
            <small>Docker execution</small>
            <strong>{{ preflight.docker.execution_mode }}</strong>
            <span>{{ preflight.docker.daemon_available ? `daemon ${preflight.docker.version || 'ready'}` : 'daemon unavailable' }}</span>
          </div>
          <div class="readiness" :class="preflight.docker.image_pinned ? 'ready' : 'blocked'">
            <small>image pin</small>
            <strong>{{ preflight.docker.image_pinned ? 'pinned' : 'local smoke marker' }}</strong>
            <span>{{ preflight.docker.image }}</span>
          </div>
        </div>
        <div class="suite-list">
          <div v-for="suite in suites" :key="suite.suite" class="suite-row">
            <strong>{{ suite.suite }}</strong>
            <span>{{ suite.cases }} cases</span>
            <span :class="suite.public_code ? 'warning' : 'safe'">{{ suite.public_code ? 'public / gated' : 'local / offline' }}</span>
          </div>
        </div>
      </section>

      <section class="card evaluation-card">
        <div class="card-heading">
          <div>
            <p class="label">SAFETY MATRIX</p>
            <h2>Failure taxonomy</h2>
          </div>
          <span class="badge">12 deterministic hooks</span>
        </div>
        <div class="taxonomy">
          <span>required-check mismatch</span><span>stale evidence / HEAD</span><span>dirty snapshot</span>
          <span>invalid tool / path traversal</span><span>risky command approval</span><span>timeout / flood / cancel</span>
          <span>restart recovery</span><span>event / receipt tamper</span><span>budget exhaustion</span>
        </div>
        <p class="hint">Local smoke is labeled explicitly. Public or real evaluation remains blocked until Docker, immutable provenance, egress, confirmation, and budget caps are all satisfied.</p>
      </section>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

const form = reactive({
  repo_path: 'benchmarks/fixtures/validation',
  goal: '',
  check_command: 'python -m pytest -q',
  max_iterations: 3,
  max_steps: 32,
  provider: { base_url: '', model: '', api_key: '', transport: 'auto' }
})
const currentTask = ref(null)
const history = ref([])
const events = ref([])
const receipt = ref(null)
const busy = ref(false)
const healthOk = ref(false)
const healthText = ref('检查中')
const healthData = ref(null)
const suites = ref([])
const preflight = ref(null)
let stream = null

const terminal = computed(() => ['awaiting_apply', 'completed', 'failed', 'failed_recoverable', 'interrupted', 'cancelled'].includes(currentTask.value?.status))
const receiptVerified = computed(() => receipt.value?.verified === true)

onMounted(async () => {
  try {
    const saved = localStorage.getItem('patchproof.provider')
    if (saved) {
      try { Object.assign(form.provider, JSON.parse(saved)) } catch { /* keep defaults */ }
    }
    const response = await fetch('/api/health')
    const data = await response.json()
    healthData.value = data
    healthOk.value = data.status === 'ok'
    healthText.value = data.llm_enabled ? 'API · LLM ready' : 'API · missing key'
    if (data.default_repo) form.repo_path = data.default_repo
    await loadEvaluation()
    await loadTasks(true)
  } catch {
    healthText.value = 'API offline'
  }
})

async function loadEvaluation() {
  try {
    const [suiteResponse, preflightResponse] = await Promise.all([fetch('/api/suites'), fetch('/api/preflight?include_public=true')])
    if (suiteResponse.ok) suites.value = (await suiteResponse.json()).suites || []
    if (preflightResponse.ok) preflight.value = await preflightResponse.json()
  } catch {
    preflight.value = null
  }
}

async function loadTasks(openFirst = false) {
  const response = await fetch('/api/tasks')
  if (!response.ok) return
  history.value = await response.json()
  if (openFirst && history.value.length) selectTask(history.value[0])
}

function selectTask(task) {
  currentTask.value = task
  events.value = task.events || []
  receipt.value = task.receipt || null
  connectStream(task.id)
}

async function createTask() {
  busy.value = true
  events.value = []
  receipt.value = null
  localStorage.setItem('patchproof.provider', JSON.stringify(form.provider))
  const payload = { ...form }
  if (!payload.provider.base_url && !payload.provider.model && !payload.provider.api_key && payload.provider.transport === 'auto') {
    delete payload.provider
  }
  try {
    const response = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      const detail = payload?.detail
      let message = '创建任务失败'
      if (typeof detail === 'string') message = detail
      else if (Array.isArray(detail) && detail.length)
        message = detail.map((item) => (typeof item?.msg === 'string' ? item.msg : JSON.stringify(item))).join('；')
      else if (detail && typeof detail === 'object') message = JSON.stringify(detail)
      throw new Error(message)
    }
    currentTask.value = await response.json()
    history.value = [currentTask.value, ...history.value.filter((item) => item.id !== currentTask.value.id)]
    connectStream(currentTask.value.id)
  } catch (error) {
    window.alert(error.message)
    busy.value = false
  }
}

function connectStream(taskId) {
  stream?.close()
  const cursor = events.value.length ? events.value[events.value.length - 1].seq : 0
  stream = new EventSource(`/api/tasks/${taskId}/stream?after=${cursor}`)
  stream.onmessage = async (message) => {
    const event = JSON.parse(message.data)
    if (!events.value.some((item) => item.seq === event.seq)) events.value.push(event)
    await refreshTask(taskId)
    if (terminal.value) {
      stream.close()
      busy.value = false
    }
  }
  stream.onerror = () => {
    stream.close()
    refreshTask(taskId)
    busy.value = false
  }
}

async function refreshTask(taskId = currentTask.value?.id) {
  if (!taskId) return
  const response = await fetch(`/api/tasks/${taskId}`)
  if (response.ok) {
    currentTask.value = await response.json()
    events.value = currentTask.value.events || events.value
    receipt.value = currentTask.value.receipt || receipt.value
    history.value = [currentTask.value, ...history.value.filter((item) => item.id !== taskId)]
  }
}

async function approveCommand(approved) {
  await fetch(`/api/tasks/${currentTask.value.id}/approve-command`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approved, approval_id: currentTask.value.pending_approval_id })
  })
  await refreshTask()
}

async function applyTask() {
  if (!window.confirm('确认将 Receipt 对应的隔离修改写回真实仓库？')) return
  const response = await fetch(`/api/tasks/${currentTask.value.id}/apply`, { method: 'POST' })
  if (!response.ok) window.alert((await response.json()).detail || 'Apply 被拒绝')
  await refreshTask()
  busy.value = false
}

async function cancelTask() {
  await fetch(`/api/tasks/${currentTask.value.id}/cancel`, { method: 'POST' })
  await refreshTask()
  busy.value = false
}

function formatTime(value) {
  return value ? new Date(value).toLocaleTimeString() : ''
}

function shortHash(value) {
  return value ? `${value.slice(0, 10)}…` : 'no hash'
}
</script>
