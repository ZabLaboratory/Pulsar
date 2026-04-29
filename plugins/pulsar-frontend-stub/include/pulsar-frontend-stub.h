#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void pulsar_frontend_init(void);
void pulsar_frontend_finished_loading(void);
void pulsar_frontend_shutdown(void);

#ifdef __cplusplus
}
#endif
