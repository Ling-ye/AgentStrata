import {app, dialog} from 'electron';
import {startDesktop} from './desktop';

void startDesktop().catch(error => {
  dialog.showErrorBox('AgentStrata Live2D', error instanceof Error ? error.message : '启动失败');
  app.quit();
});
