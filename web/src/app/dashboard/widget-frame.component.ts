import { NgComponentOutlet } from '@angular/common';
import { Component, EventEmitter, Input, Output } from '@angular/core';

import { WidgetInstance, WidgetStyle } from './models/dashboard.types';
import { WidgetRegistryService } from './widget-registry.service';

/** Chrome (title bar, style switch, maximize, remove) wrapping a dynamically-rendered widget component. */
@Component({
  selector: 'app-widget-frame',
  standalone: true,
  imports: [NgComponentOutlet],
  templateUrl: './widget-frame.component.html',
  styleUrl: './widget-frame.component.scss'
})
export class WidgetFrameComponent {
  @Input({ required: true }) instance!: WidgetInstance;
  @Input() maximized = false;

  @Output() maximizeToggle = new EventEmitter<void>();
  @Output() remove = new EventEmitter<void>();
  @Output() styleChange = new EventEmitter<WidgetStyle>();

  constructor(private registry: WidgetRegistryService) {}

  get entry() {
    return this.registry.find(this.instance.type, this.instance.style);
  }

  get availableStyles(): WidgetStyle[] {
    return this.registry.stylesFor(this.instance.type);
  }

  onStyleChange(event: Event): void {
    const style = (event.target as HTMLSelectElement).value as WidgetStyle;
    this.styleChange.emit(style);
  }
}
