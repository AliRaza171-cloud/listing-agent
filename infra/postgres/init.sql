-- Runs once when the Postgres container is first created: one database per service.
CREATE DATABASE auth;
CREATE DATABASE catalog;
CREATE DATABASE store;
CREATE DATABASE publisher;
CREATE DATABASE billing;
CREATE DATABASE notification;
