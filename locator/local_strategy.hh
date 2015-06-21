/*
 * Copyright (C) 2015 Cloudius Systems, Ltd.
 */

#pragma once

#include "abstract_replication_strategy.hh"

// forward declaration since database.hh includes this file
class keyspace;

namespace locator {

using inet_address = gms::inet_address;
using token = dht::token;

class local_strategy : public abstract_replication_strategy {
protected:
    virtual std::vector<inet_address> calculate_natural_endpoints(const token& search_token);
public:
    local_strategy(const sstring& keyspace_name, token_metadata& token_metadata, snitch_ptr&& snitch, const std::map<sstring, sstring>& config_options);
    virtual ~local_strategy() {};
    virtual size_t get_replication_factor() const;
};

}
