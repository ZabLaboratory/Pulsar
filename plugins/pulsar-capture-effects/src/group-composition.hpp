#pragma once

#include <obs-data.h>

void group_composition_request(obs_data_t *request, obs_data_t *response,
                               void *private_data);
void reset_group_composition_request(obs_data_t *request, obs_data_t *response,
                                     void *private_data);
bool group_composition_load();
void group_composition_unload();
