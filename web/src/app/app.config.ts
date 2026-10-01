import { HttpClient, provideHttpClient, withFetch } from '@angular/common/http';
import { APP_INITIALIZER, ApplicationConfig, /* inject, */ provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';

import { routes } from './app.routes';
import { AppConfigService } from './core/config/app-config.service';
/* import { firstValueFrom } from 'rxjs/internal/firstValueFrom';
import { bootstrapApplication } from '@angular/platform-browser'; */

function initAppConfig(config: AppConfigService): () => Promise<void> {
  return () => config.load();
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(withFetch()),
    {
      provide: APP_INITIALIZER,
      useFactory: initAppConfig,
      deps: [AppConfigService],
      multi: true
    }
  ]
};
/* 
function initializeApp() {
  const http: HttpClient = inject(HttpClient);
  return firstValueFrom(
    http
      .get("https://someUrl.com/api/user")
      .pipe()
  );
}

bootstrapApplication(AppConfigService, {
  providers: [
    provideHttpClient(withFetch()),
    {
      provide: APP_INITIALIZER,
      useFactory: initializeApp,
      deps: [HttpClient],
      multi: true
    }
  ]
}); */
  /** * ### Example with standalone application
 * ```ts
 * function initializeApp() {
 *   const http = inject(HttpClient);
 *   return firstValueFrom(
 *     http
 *       .get("https://someUrl.com/api/user")
 *       .pipe(tap(user => { ... }))
 *   );
 * }
 *
 * bootstrapApplication(App, {
 *   providers: [
 *     provideHttpClient(),
 *     {
 *       provide: APP_INITIALIZER,
 *       useValue: initializeApp,
 *       multi: true,
 *     },
 *   ],
 * });
 * ``` */


