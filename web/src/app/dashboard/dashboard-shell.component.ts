import { DecimalPipe } from '@angular/common';
import { Component, OnInit } from '@angular/core';
import { GridsterConfig, GridsterItem, GridsterModule } from 'angular-gridster2';

import { CaptureStateService } from '../core/recording/capture-state.service';
import { AddWidgetPaletteComponent } from './add-widget-palette.component';
import { DashboardLayout, WidgetInstance, WidgetStyle } from './models/dashboard.types';
import { LayoutStoreService } from './layout-store.service';
import { WidgetFrameComponent } from './widget-frame.component';

/** Top-level dashboard: grid of draggable/resizable widgets, maximize handling, multi-dashboard switching. */
@Component({
  selector: 'app-dashboard-shell',
  standalone: true,
  imports: [DecimalPipe, GridsterModule, WidgetFrameComponent, AddWidgetPaletteComponent],
  templateUrl: './dashboard-shell.component.html',
  styleUrl: './dashboard-shell.component.scss'
})
export class DashboardShellComponent implements OnInit {
  dashboards: DashboardLayout[] = [];
  activeDashboard!: DashboardLayout;
  maximizedWidgetId: string | null = null;
  showPalette = false;

  options: GridsterConfig = {
    gridType: 'fit',
    compactType: 'none',
    margin: 8,
    outerMargin: true,
    minCols: 12,
    maxCols: 12,
    minRows: 8,
    fixedRowHeight: 90,
    mobileBreakpoint: 720,
    mobileModeEnabled: true,
    keepFixedHeightInMobile: false,
    fixedRowHeightOnMobile: 110,
    draggable: {
      enabled: true,
      ignoreContent: true,
      dragHandleClass: 'widget-frame__drag-handle'
    },
    resizable: { enabled: true },
    pushItems: true,
    itemChangeCallback: () => this.persist()
  };

  constructor(private store: LayoutStoreService, public captureState: CaptureStateService) {}

  ngOnInit(): void {
    this.dashboards = this.store.listDashboards();
    const activeName = this.store.getActiveName();
    this.activeDashboard =
      this.dashboards.find((d) => d.name === activeName) ?? this.dashboards[0];
  }

  selectDashboard(name: string): void {
    const dashboard = this.dashboards.find((d) => d.name === name);
    if (dashboard) {
      this.activeDashboard = dashboard;
      this.store.setActiveName(name);
    }
  }

  openCaptureStudio(): void {
    this.selectDashboard('Capture Studio');
  }

  createDashboard(): void {
    const name = prompt('New dashboard name?');
    if (!name || this.dashboards.some((d) => d.name === name)) {
      return;
    }
    const created: DashboardLayout = { name, widgets: [] };
    this.dashboards.push(created);
    this.store.save(created);
    this.selectDashboard(name);
  }

  deleteDashboard(name: string): void {
    if (this.dashboards.length <= 1) {
      return;
    }
    this.store.delete(name);
    this.dashboards = this.dashboards.filter((d) => d.name !== name);
    this.selectDashboard(this.dashboards[0].name);
  }

  addWidget(instance: WidgetInstance): void {
    this.activeDashboard.widgets.push(instance);
    this.persist();
  }

  removeWidget(id: string): void {
    this.activeDashboard.widgets = this.activeDashboard.widgets.filter((w) => w.id !== id);
    if (this.maximizedWidgetId === id) {
      this.maximizedWidgetId = null;
    }
    this.persist();
  }

  changeWidgetStyle(widget: WidgetInstance, style: WidgetStyle): void {
    widget.style = style;
    this.persist();
  }

  toggleMaximize(id: string): void {
    this.maximizedWidgetId = this.maximizedWidgetId === id ? null : id;
  }

  asGridsterItem(widget: WidgetInstance): GridsterItem {
    return widget as unknown as GridsterItem;
  }

  private persist(): void {
    this.store.updateWidgets(this.activeDashboard.name, this.activeDashboard.widgets);
  }
}
