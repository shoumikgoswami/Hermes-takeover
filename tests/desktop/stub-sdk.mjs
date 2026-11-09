export const calls = { notify: [], native: [], pet: [], run: [], rest: [], workspaces: [] }
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const PALETTE_AREA = 'palette'
export const host = {
  notify: n => { calls.notify.push(n); return 'n' + calls.notify.length },
  navigate: p => calls.run.push('navigate:' + p),
  openWorkspace: (id, o) => { calls.workspaces.push({ id, ...o }); return () => {} }
}
