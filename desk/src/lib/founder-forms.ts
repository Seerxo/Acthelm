// The Desk's two founder create forms as pure functions: what is sent, or why nothing is. The API re-checks all of it
// (POST /stage-c/tasks, POST /goals: schemas.StageCTask, schemas.GoalCreate); this only refuses early and says why.
export type Built<T> = { body: T; error: null } | { body: null; error: string }
const no = (error: string) => ({ body: null, error })
const len = (s: string, min: number, max: number) => s.length >= min && s.length <= max

export interface CodeTaskForm { title: string; objective: string; success: string; paths: string[]; context: string }
export interface CodeTaskBody { title: string; objective: string; success_criteria: string; paths: string[]; context: string[] }

/** A Stage C code task: write paths only from `allowed` (GET /stage-c/state code_task_paths); context is one path per
 *  line, read-only, app/ or tests/ Python files (stage_c_worker.scope_refused). */
export function codeTaskBody(f: CodeTaskForm, allowed: readonly string[]): Built<CodeTaskBody> {
  const title = f.title.trim(), objective = f.objective.trim(), success = f.success.trim()
  if (!len(title, 3, 200)) return no("Title needs 3 to 200 characters.")
  if (!len(objective, 3, 2000)) return no("Objective needs 3 to 2000 characters.")
  if (!len(success, 3, 2000)) return no("Success criteria need 3 to 2000 characters.")
  const paths = [...new Set(f.paths)]
  if (!paths.length) return no("Choose at least one file the task may change.")
  if (paths.some(p => !allowed.includes(p))) return no("A chosen file is not an allowed Stage C file.")
  const context = [...new Set(f.context.split(/[\s,]+/).filter(Boolean))]
  if (context.some(p => !/^(app|tests)\/[A-Za-z0-9_./-]+\.py$/.test(p) || p.includes("..")))
    return no("Context files must be app/ or tests/ Python files.")
  if (context.some(p => paths.includes(p))) return no("A context file is already a file the task may change.")
  if (context.length > 10 || paths.length + context.length > 20) return no("At most 10 context files and 20 files in all.")
  return { body: { title, objective, success_criteria: success, paths, context }, error: null }
}

export interface GoalForm { title: string; metric: string; target: string; baseline: string; project: string }
export interface GoalBody { title: string; metric: string; target: string; baseline?: string; project?: string }

/** A PROPOSED goal (the API default): title, metric and target are required, baseline optional. */
export function goalBody(f: GoalForm): Built<GoalBody> {
  const title = f.title.trim(), metric = f.metric.trim(), target = f.target.trim(), baseline = f.baseline.trim()
  if (!len(title, 3, 300)) return no("Title needs 3 to 300 characters.")
  if (!len(metric, 2, 200)) return no("Metric needs 2 to 200 characters.")
  if (!len(target, 1, 100)) return no("Target needs 1 to 100 characters.")
  if (baseline.length > 100) return no("Baseline is at most 100 characters.")
  return { body: { title, metric, target, ...(baseline && { baseline }), ...(f.project && { project: f.project }) }, error: null }
}
