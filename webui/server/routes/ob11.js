import express from 'express';
import { state } from '../state.js';

export const ob11Router = express.Router();

ob11Router.post('/events', (req, res) => {
  const event = req.body;
  if (event && event.post_type) {
    state.recordEvent(event);
  }
  res.json({ status: 'ok' });
});
